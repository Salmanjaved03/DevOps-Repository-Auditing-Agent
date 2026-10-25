"""Inspection tools for the DevOps Repository Auditing Agent.

Each tool is a plain function that operates on a locally-cloned repository
directory.  The TOOL_REGISTRY at the bottom maps tool names to their
implementation and JSON-schema description (used by the LLM for
function-calling).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

# Maximum bytes returned from read_file to respect LLM context budgets.
_MAX_READ_BYTES = 12_000
# Maximum search results returned by search_code.
_MAX_SEARCH_RESULTS = 20
# Maximum commits returned by git_log.
_MAX_COMMITS = 15


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_git(repo_dir: str, *args: str, timeout: int = 30) -> str:
    """Run a git command inside *repo_dir* and return its stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return f"[git error] {result.stderr.strip()}"
    return result.stdout


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def list_files(repo_dir: str, **_kwargs: Any) -> str:
    """Return a listing of files in the repository.

    For small repositories, lists all files. For large repositories, prioritizes
    configuration, workflow, test, and dependency files while summarizing
    bulk source directories to preserve context window.
    """
    entries: list[str] = []
    repo_path = Path(repo_dir)

    priority_dirs = {".github", ".circleci", ".gitlab", "tests", "test", "ci"}
    priority_files = {
        "readme.md", "readme.rst", "readme.txt", "readme",
        "license", "license.txt", "license.rst", "license.md",
        ".gitignore", ".gitattributes", ".editorconfig",
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
        "requirements-dev.txt", "pipfile", "pipfile.lock", "poetry.lock",
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "makefile", "tox.ini", ".pre-commit-config.yaml",
    }

    all_files: list[tuple[Path, Path]] = []
    for root, dirs, files in os.walk(repo_dir):
        # Skip .git internals
        dirs[:] = [d for d in dirs if d != ".git"]
        for fname in sorted(files):
            full = Path(root) / fname
            rel = full.relative_to(repo_path)
            all_files.append((rel, full))

    if not all_files:
        return "(empty repository)"

    # If small repo (<= 80 files), list everything
    if len(all_files) <= 80:
        for rel, full in all_files:
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            entries.append(f"{rel}  ({size} bytes)")
        return "\n".join(entries)

    # For larger repos, prioritize root and configuration files
    prioritized: list[tuple[Path, Path]] = []
    others: list[tuple[Path, Path]] = []

    for rel, full in all_files:
        parts = rel.parts
        is_root = len(parts) == 1
        is_priority_dir = len(parts) > 1 and parts[0].lower() in priority_dirs
        is_priority_file = rel.name.lower() in priority_files

        if is_root or is_priority_dir or is_priority_file:
            prioritized.append((rel, full))
        else:
            others.append((rel, full))

    # Add prioritized files first
    for rel, full in prioritized[:70]:
        try:
            size = full.stat().st_size
        except OSError:
            size = 0
        entries.append(f"{rel}  ({size} bytes)")

    # Add a sample of other files
    remaining_slots = max(0, 80 - len(entries))
    for rel, full in others[:remaining_slots]:
        try:
            size = full.stat().st_size
        except OSError:
            size = 0
        entries.append(f"{rel}  ({size} bytes)")

    skipped = len(all_files) - len(entries)
    if skipped > 0:
        entries.append(f"\n[... and {skipped} other files across repository directories]")

    return "\n".join(entries)


def read_file(repo_dir: str, path: str, **_kwargs: Any) -> str:
    """Read the contents of a file. Returns line-numbered content."""
    target = Path(repo_dir) / path
    if not target.exists():
        return f"[error] File not found: {path}"
    if not target.is_file():
        return f"[error] Not a regular file: {path}"

    # Security check: resolve and verify path stays within repo
    try:
        target.resolve().relative_to(Path(repo_dir).resolve())
    except ValueError:
        return "[error] Path escapes the repository directory."

    try:
        raw = target.read_bytes()[:_MAX_READ_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        is_truncated = len(raw) == _MAX_READ_BYTES
        lines = text.split("\n")
        numbered = [f"{i+1:>4} | {line}" for i, line in enumerate(lines)]
        output = "\n".join(numbered)
        if is_truncated:
            output += f"\n\n[truncated — file exceeds {_MAX_READ_BYTES // 1000} KB]"
        return output
    except Exception as exc:
        return f"[error] Could not read {path}: {exc}"


def search_code(repo_dir: str, pattern: str, **_kwargs: Any) -> str:
    """Search for a regex pattern across files in the repository."""
    results: list[str] = []
    repo_path = Path(repo_dir)
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"[error] Invalid regex pattern: {exc}"

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fname in sorted(files):
            full = Path(root) / fname
            rel = str(full.relative_to(repo_path))
            try:
                raw = full.read_bytes()[:_MAX_READ_BYTES]
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue  # skip binary
                for lineno, line in enumerate(text.split("\n"), start=1):
                    if regex.search(line):
                        # Cap line length to 160 chars
                        line_str = line.rstrip()
                        if len(line_str) > 160:
                            line_str = line_str[:157] + "..."
                        results.append(f"{rel}:{lineno}: {line_str}")
                        if len(results) >= _MAX_SEARCH_RESULTS:
                            results.append(
                                f"[truncated — showing first {_MAX_SEARCH_RESULTS} matches]"
                            )
                            return "\n".join(results)
            except OSError:
                continue

    if not results:
        return f"No matches found for pattern: {pattern}"
    return "\n".join(results)


def git_log(repo_dir: str, max_commits: int | None = None, **_kwargs: Any) -> str:
    """Return recent commit history."""
    n = min(max_commits or _MAX_COMMITS, _MAX_COMMITS)
    return _run_git(
        repo_dir,
        "log",
        f"-n{n}",
        "--pretty=format:%h %ae %ad %s",
        "--date=short",
    )


def check_github_actions(repo_dir: str, **_kwargs: Any) -> str:
    """List and summarise GitHub Actions workflow files."""
    workflows_dir = Path(repo_dir) / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return "No .github/workflows directory found."

    results: list[str] = []
    for wf in sorted(workflows_dir.iterdir()):
        if wf.suffix in (".yml", ".yaml"):
            try:
                content = wf.read_text(encoding="utf-8", errors="replace")
                # Truncate very long workflow files
                if len(content) > 3_000:
                    content = content[:3_000] + "\n... [workflow truncated]"
                results.append(f"=== {wf.name} ===\n{content}\n")
            except OSError as exc:
                results.append(f"=== {wf.name} === [read error: {exc}]\n")

    if not results:
        return "No YAML workflow files found in .github/workflows/."
    return "\n".join(results)


def list_branches(repo_dir: str, **_kwargs: Any) -> str:
    """List local and remote-tracking branches."""
    return _run_git(repo_dir, "branch", "-a")


def check_dependencies(repo_dir: str, **_kwargs: Any) -> str:
    """Inspect dependency/lockfile presence and version pinning."""
    repo_path = Path(repo_dir)
    report_parts: list[str] = []

    dep_files = [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements_dev.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Gemfile",
        "Gemfile.lock",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "composer.json",
        "composer.lock",
    ]

    found: list[str] = []
    for df in dep_files:
        if (repo_path / df).exists():
            found.append(df)
    report_parts.append(
        "Dependency-related files found: " + (", ".join(found) if found else "(none)")
    )

    # Check for unpinned deps in requirements*.txt
    for req_file in ["requirements.txt", "requirements-dev.txt", "requirements_dev.txt"]:
        req_path = repo_path / req_file
        if req_path.exists():
            lines = req_path.read_text(errors="replace").splitlines()
            unpinned = []
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith("#")
                    and not stripped.startswith("-")
                ):
                    if "==" not in stripped and ">=" not in stripped and "<=" not in stripped:
                        unpinned.append(f"  line {i}: {stripped}")
            if unpinned:
                report_parts.append(f"\nUnpinned dependencies in {req_file}:")
                report_parts.extend(unpinned[:15])
            else:
                report_parts.append(f"\nAll dependencies in {req_file} appear pinned.")

    # Check lockfile pairing
    pairs = [
        ("Pipfile", ["Pipfile.lock"]),
        ("package.json", ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]),
        ("Gemfile", ["Gemfile.lock"]),
        ("go.mod", ["go.sum"]),
        ("Cargo.toml", ["Cargo.lock"]),
        ("composer.json", ["composer.lock"]),
    ]
    for manifest, lockfiles in pairs:
        if (repo_path / manifest).exists():
            has_lock = any((repo_path / lf).exists() for lf in lockfiles)
            if not has_lock:
                report_parts.append(
                    f"\n⚠ {manifest} found but no lockfile ({', '.join(lockfiles)})."
                )

    return "\n".join(report_parts)


# ---------------------------------------------------------------------------
# Tool registry — maps names to (function, JSON-schema description)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "list_files": {
        "function": list_files,
        "description": "List repository files and directory structure with sizes.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "read_file": {
        "function": read_file,
        "description": "Read line-numbered contents of a file (e.g. .gitignore, pyproject.toml, workflow file).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file within the repository.",
                }
            },
            "required": ["path"],
        },
    },
    "search_code": {
        "function": search_code,
        "description": "Search for a regex pattern across files (e.g. hardcoded secrets, dangerous commands).",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "A Python regular expression pattern to search for.",
                }
            },
            "required": ["pattern"],
        },
    },
    "git_log": {
        "function": git_log,
        "description": "Show recent git commit history (hash, author, date, message).",
        "parameters": {
            "type": "object",
            "properties": {
                "max_commits": {
                    "type": "integer",
                    "description": "Maximum number of commits to show (default 15).",
                }
            },
            "required": [],
        },
    },
    "check_github_actions": {
        "function": check_github_actions,
        "description": "List and inspect all GitHub Actions workflow YAML files in .github/workflows/.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "list_branches": {
        "function": list_branches,
        "description": "List local and remote-tracking git branches.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "check_dependencies": {
        "function": check_dependencies,
        "description": "Analyze dependency files (requirements.txt, pyproject.toml, package.json, etc.) for pinning and lockfiles.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}
