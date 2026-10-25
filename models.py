"""Data models for the DevOps Repository Auditing Agent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional, Any


# ---------------------------------------------------------------------------
# Violation & Report
# ---------------------------------------------------------------------------

ALLOWED_SEVERITIES = {"critical", "high", "medium", "low"}

ALLOWED_CATEGORIES = {
    "repository_hygiene",
    "dependency_management",
    "code_quality",
    "testing",
    "ci_cd",
    "security",
    "configuration",
    "collaboration",
    "build_release",
}

# Mapping common variations or titles to standardized category names
_CATEGORY_NORMALIZATION = {
    "repository_hygiene": "repository_hygiene",
    "hygiene": "repository_hygiene",
    "repository": "repository_hygiene",
    "repo_hygiene": "repository_hygiene",
    "dependency_management": "dependency_management",
    "dependencies": "dependency_management",
    "dependency": "dependency_management",
    "code_quality": "code_quality",
    "quality": "code_quality",
    "testing": "testing",
    "tests": "testing",
    "test": "testing",
    "ci_cd": "ci_cd",
    "ci": "ci_cd",
    "cd": "ci_cd",
    "github_actions": "ci_cd",
    "security": "security",
    "sec": "security",
    "configuration": "configuration",
    "config": "configuration",
    "collaboration": "collaboration",
    "collab": "collaboration",
    "build_release": "build_release",
    "build": "build_release",
    "release": "build_release",
    "build_and_release": "build_release",
}


@dataclass
class Violation:
    """A single DevOps violation found in the repository."""

    rule_id: str
    category: str
    severity: str
    description: str
    evidence: str
    recommendation: str
    file: Optional[str] = None
    line: Optional[int] = None

    def __post_init__(self) -> None:
        # Normalize and validate severity
        if isinstance(self.severity, str):
            self.severity = self.severity.lower().strip()
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"severity must be one of {ALLOWED_SEVERITIES}, got '{self.severity}'"
            )

        # Normalize category
        if isinstance(self.category, str):
            clean_cat = (
                self.category.lower()
                .strip()
                .replace(" ", "_")
                .replace("-", "_")
                .replace("/", "_")
            )
            self.category = _CATEGORY_NORMALIZATION.get(clean_cat, clean_cat)

        # Normalize line number: 0 or negative becomes None
        if self.line is not None:
            try:
                self.line = int(self.line)
                if self.line <= 0:
                    self.line = None
            except (ValueError, TypeError):
                self.line = None

        if self.file is not None and not str(self.file).strip():
            self.file = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["file"] is None:
            d["file"] = None
        if d["line"] is None:
            d["line"] = None
        return d


@dataclass
class Report:
    """Full audit report for a repository."""

    repository: str
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "violations": [v.to_dict() for v in self.violations],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Tool-call models (used by the agent loop)
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """Represents the LLM's decision to invoke a tool."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Result returned after executing a tool."""

    name: str
    output: str
    error: Optional[str] = None
