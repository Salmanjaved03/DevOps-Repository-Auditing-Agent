"""LLM interface using Groq (free tier).

Uses the openai-compatible SDK to call Groq's models with
tool/function-calling support. Handles Groq's free-tier rate limits
with smart retry, backoff, and dynamic model fallback.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from openai import OpenAI, APIStatusError

from tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

# Groq models — ordered by reliability & speed on free tier.
DEFAULT_MODEL = "qwen/qwen3.8-27b"

_MODEL_FALLBACK_CHAIN = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
]

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class PayloadTooLargeError(Exception):
    """Raised when the request payload exceeds Groq's TPM limit."""
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_tool_definitions() -> list[dict[str, Any]]:
    """Convert TOOL_REGISTRY into OpenAI-style tool definitions."""
    tools: list[dict[str, Any]] = []
    for name, meta in TOOL_REGISTRY.items():
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": meta["description"],
                "parameters": meta["parameters"],
            },
        })
    return tools


def _extract_retry_after(exc: Exception) -> int | None:
    """Extract retry-after seconds from error message if present."""
    msg = str(exc)
    m = re.search(r'retry.after.*?(\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'try again in (\d+)', msg, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# GroqLLM class
# ---------------------------------------------------------------------------

class GroqLLM:
    """Wrapper around the Groq API for the auditing agent."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "GROQ_API_KEY is required. Set it in .env or pass it directly."
            )
        self._requested_model = model or os.getenv("GROQ_MODEL", "")
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=GROQ_BASE_URL,
        )
        self._tool_definitions = _build_tool_definitions()
        self.model_index = 0
        self.model_name = self._resolve_model()
        logger.info("Using Groq model: %s", self.model_name)

    def _resolve_model(self) -> str:
        """Find a working model from the fallback chain."""
        if self._requested_model:
            return self._requested_model

        for idx, model_name in enumerate(_MODEL_FALLBACK_CHAIN):
            try:
                logger.info("Probing model: %s", model_name)
                self.client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": "Say OK"}],
                    max_tokens=5,
                )
                logger.info("Model %s is available ✓", model_name)
                self.model_index = idx
                return model_name
            except Exception as exc:
                logger.warning("Model %s unavailable: %s", model_name, exc)
                continue

        return _MODEL_FALLBACK_CHAIN[0]

    def _switch_to_next_model(self) -> str | None:
        """Switch to the next available model in the fallback chain."""
        if self._requested_model:
            return None  # User pinned a specific model

        next_idx = self.model_index + 1
        if next_idx < len(_MODEL_FALLBACK_CHAIN):
            self.model_index = next_idx
            self.model_name = _MODEL_FALLBACK_CHAIN[next_idx]
            logger.warning("Switching to fallback model: %s", self.model_name)
            return self.model_name
        return None

    # ---- public API --------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        max_tokens: int = 600,
        max_retries: int = 6,
    ) -> Any:
        """Send a chat request to Groq with tool-calling enabled.

        Parameters
        ----------
        messages : list[dict]
            Conversation history in OpenAI message format.
        system_prompt : str
            System instruction for the model.
        max_tokens : int
            Max completion tokens. Defaults to 600 for tool calls, 2048 for report.
        max_retries : int
            Max retries on transient errors.

        Returns
        -------
        openai ChatCompletion response
        """
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        last_err: Exception | None = None
        for attempt in range(1, max_retries + 1):
            kwargs: dict[str, Any] = {
                "model": self.model_name,
                "messages": full_messages,
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "tools": self._tool_definitions,
                "tool_choice": "auto",
            }

            try:
                return self.client.chat.completions.create(**kwargs)
            except APIStatusError as exc:
                last_err = exc
                status = exc.status_code

                if status == 413:
                    logger.warning("Groq 413: Payload too large. Bubbling up to trim context.")
                    raise PayloadTooLargeError(str(exc)) from exc

                if status == 429:
                    retry_after = _extract_retry_after(exc)
                    # If retry_after is huge (> 60s), switch to fallback model immediately
                    if retry_after and retry_after > 60:
                        logger.warning("Groq rate limit retry_after is %ds.", retry_after)
                        if self._switch_to_next_model():
                            continue

                    wait = retry_after if retry_after and retry_after <= 60 else min(5 * attempt, 30)
                    logger.warning(
                        "Groq rate limit (%s, attempt %d/%d) — waiting %ds",
                        self.model_name, attempt, max_retries, wait,
                    )
                    time.sleep(wait)
                else:
                    wait = min(2 ** attempt, 30)
                    logger.warning(
                        "Groq API error %d (%s, attempt %d/%d): %s — retrying in %ds",
                        status, self.model_name, attempt, max_retries, exc, wait,
                    )
                    time.sleep(wait)

            except Exception as exc:
                last_err = exc
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "Groq API error (%s, attempt %d/%d): %s — retrying in %ds",
                    self.model_name, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)

        raise RuntimeError(
            f"Groq API failed after {max_retries} attempts: {last_err}"
        ) from last_err
