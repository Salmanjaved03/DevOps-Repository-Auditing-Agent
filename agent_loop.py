"""Core agent reasoning and tool-dispatch loop.

Implements the cycle:
    Observe → Reason → Select Tool → Execute → Interpret → Report
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from llm import GroqLLM, PayloadTooLargeError
from models import Report, Violation, ToolResult
from prompts import SYSTEM_PROMPT, INITIAL_USER_MESSAGE
from tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 4           # 2-3 inspection rounds + report generation
MAX_TOOL_OUTPUT_LEN = 2_500  # truncate individual tool outputs to preserve token budget
_MAX_CONV_CHARS = 10_000     # ~2500 tokens max conversation size for Groq 8k TPM limit


# ---------------------------------------------------------------------------
# Repo cloning
# ---------------------------------------------------------------------------

def clone_repo(repo_url: str, dest: str) -> None:
    """Shallow-clone a public GitHub repo into *dest*."""
    logger.info("Cloning %s → %s", repo_url, dest)
    result = subprocess.run(
        ["git", "clone", "--depth=50", repo_url, dest],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------

def _parse_report(text: str, repo_url: str) -> Report:
    """Extract a valid Report from the model's final text output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.index("\n")
        cleaned = cleaned[first_nl + 1:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                raise ValueError(f"Could not parse JSON from model output: {exc}") from exc
        else:
            raise ValueError(f"No JSON object found in model output: {exc}") from exc

    violations: list[Violation] = []
    for v in data.get("violations", []):
        try:
            violations.append(
                Violation(
                    rule_id=str(v.get("rule_id", "DEV-000")),
                    category=str(v.get("category", "repository_hygiene")),
                    severity=str(v.get("severity", "medium")),
                    description=str(v.get("description", "")),
                    evidence=str(v.get("evidence", "")),
                    recommendation=str(v.get("recommendation", "")),
                    file=v.get("file"),
                    line=v.get("line"),
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping invalid violation: %s — %s", v, exc)

    return Report(repository=repo_url, violations=violations)


# ---------------------------------------------------------------------------
# Execute a single tool call
# ---------------------------------------------------------------------------

def _execute_tool(name: str, arguments: dict[str, Any], repo_dir: str) -> ToolResult:
    """Look up *name* in TOOL_REGISTRY and execute it."""
    meta = TOOL_REGISTRY.get(name)
    if meta is None:
        return ToolResult(name=name, output="", error=f"Unknown tool: {name}")
    func = meta["function"]
    try:
        output = func(repo_dir=repo_dir, **arguments)
        if len(output) > MAX_TOOL_OUTPUT_LEN:
            output = output[:MAX_TOOL_OUTPUT_LEN] + "\n\n[output truncated]"
        return ToolResult(name=name, output=output)
    except Exception as exc:
        logger.exception("Tool %s raised an exception", name)
        return ToolResult(name=name, output="", error=str(exc))


# ---------------------------------------------------------------------------
# Conversation Trimming (Preserving Valid OpenAI Turn Structure)
# ---------------------------------------------------------------------------

def _estimate_chars(messages: list[dict[str, Any]]) -> int:
    """Calculate total character length across all messages."""
    total = 0
    for m in messages:
        total += len(m.get("content", "") or "")
        for tc in m.get("tool_calls", []):
            total += len(tc.get("function", {}).get("arguments", ""))
    return total


def _trim_conversation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim conversation by dropping complete earlier rounds when over budget."""
    if len(messages) <= 3 or _estimate_chars(messages) <= _MAX_CONV_CHARS:
        return messages

    initial_msg = messages[0]
    rest = messages[1:]

    turns: list[list[dict[str, Any]]] = []
    current_turn: list[dict[str, Any]] = []

    for msg in rest:
        if msg.get("role") == "assistant" and current_turn:
            turns.append(current_turn)
            current_turn = [msg]
        else:
            current_turn.append(msg)
    if current_turn:
        turns.append(current_turn)

    while len(turns) > 2 and _estimate_chars([initial_msg] + [m for t in turns for m in t]) > _MAX_CONV_CHARS:
        turns.pop(0)

    flattened = [initial_msg] + [m for t in turns for m in t]
    return flattened


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def run_agent(repo_url: str, api_key: str | None = None) -> Report:
    """Clone *repo_url*, run the LLM-driven audit loop, and return a Report."""
    tmp_dir = tempfile.mkdtemp(prefix="devops_audit_")
    repo_dir = str(Path(tmp_dir) / "repo")
    try:
        clone_repo(repo_url, repo_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    try:
        return _run_loop(repo_url, repo_dir, api_key)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_loop(repo_url: str, repo_dir: str, api_key: str | None) -> Report:
    """Inner agent loop — interact with the LLM until it produces a report."""
    llm = GroqLLM(api_key=api_key)

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": INITIAL_USER_MESSAGE.format(repo_dir=repo_dir, repo_url=repo_url),
        }
    ]

    executed_calls: set[str] = set()

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info("=== Agent iteration %d / %d ===", iteration, MAX_ITERATIONS)

        messages = _trim_conversation(messages)

        # Allow more tokens once evidence has been gathered
        max_tok = 2048 if iteration >= 3 else 500

        try:
            response = llm.chat(
                messages,
                system_prompt=SYSTEM_PROMPT,
                max_tokens=max_tok,
            )
        except PayloadTooLargeError:
            logger.warning("Caught PayloadTooLargeError. Aggressively trimming history.")
            if len(messages) > 3:
                messages = [messages[0]] + messages[-2:]
            response = llm.chat(
                messages,
                system_prompt=SYSTEM_PROMPT,
                max_tokens=max_tok,
            )

        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise RuntimeError("Groq returned no choices.")

        message = choice.message
        tool_calls = message.tool_calls

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                call_key = f"{func_name}:{json.dumps(args, sort_keys=True)}"
                if call_key in executed_calls:
                    logger.info("Skipping duplicate tool call: %s", call_key)
                    output_text = (
                        f"[Notice: You have already executed {func_name} with these arguments. "
                        "Do not repeat identical calls. Please proceed to other areas or output the final JSON report.]"
                    )
                else:
                    executed_calls.add(call_key)
                    logger.info("Tool call: %s(%s)", func_name, json.dumps(args, default=str))
                    result = _execute_tool(func_name, args, repo_dir)
                    output_text = result.output if not result.error else f"[error] {result.error}"
                    logger.info("Tool result (%s): %s", func_name, output_text[:160])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": output_text,
                })

            if iteration >= 2:
                messages.append({
                    "role": "user",
                    "content": (
                        "You have inspected the key repository areas. "
                        "Please synthesize all your observations and output ONLY the final JSON report object."
                    ),
                })
        else:
            # Model returned text without tool calls
            full_text = message.content or ""
            if not full_text.strip():
                messages.append({"role": "assistant", "content": ""})
                messages.append({
                    "role": "user",
                    "content": "Please output the final JSON report now.",
                })
                continue

            logger.info("Agent produced response (length=%d)", len(full_text))
            try:
                return _parse_report(full_text, repo_url)
            except ValueError:
                logger.warning("Response not valid JSON. Prompting for report.")
                messages.append({"role": "assistant", "content": full_text})
                messages.append({
                    "role": "user",
                    "content": (
                        "Please provide ONLY the final JSON report object matching the specified schema. "
                        "Do not include any explanation or markdown formatting."
                    ),
                })

    # If max iterations reached, request final report
    logger.info("Max iterations reached. Requesting final report.")
    messages = _trim_conversation(messages)
    messages.append({
        "role": "user",
        "content": (
            "You have completed your investigation. Based on all evidence gathered, "
            "output the final JSON audit report now. Respond with ONLY valid JSON matching the schema."
        ),
    })

    response = llm.chat(
        messages,
        system_prompt=SYSTEM_PROMPT,
        max_tokens=2048,
    )
    choice = response.choices[0] if response.choices else None
    if choice and choice.message:
        if choice.message.content and choice.message.content.strip():
            try:
                return _parse_report(choice.message.content, repo_url)
            except ValueError:
                pass

        if choice.message.tool_calls:
            # Handle final tool call gracefully
            for tc in choice.message.tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                res = _execute_tool(func_name, args, repo_dir)
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                    ],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": res.output,
                })
            messages.append({
                "role": "user",
                "content": "Now deliver your final JSON report. Respond with ONLY valid JSON.",
            })
            resp2 = llm.chat(messages, system_prompt=SYSTEM_PROMPT, max_tokens=2048)
            if resp2.choices and resp2.choices[0].message and resp2.choices[0].message.content:
                return _parse_report(resp2.choices[0].message.content, repo_url)

    logger.error("Agent could not produce a report after %d iterations", MAX_ITERATIONS)
    return Report(repository=repo_url, violations=[])
