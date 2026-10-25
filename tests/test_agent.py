"""Integration tests for the agent loop (agent_loop.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from models import Report, Violation


class TestReportParsing:
    """Test the _parse_report helper."""

    def test_parse_valid_json(self) -> None:
        from agent_loop import _parse_report

        text = json.dumps({
            "repository": "https://github.com/example/demo",
            "violations": [
                {
                    "rule_id": "SEC-001",
                    "category": "security",
                    "severity": "critical",
                    "file": "config.py",
                    "line": 17,
                    "description": "Hardcoded API credential",
                    "evidence": "Credential assigned directly",
                    "recommendation": "Use env variable",
                }
            ],
        })
        report = _parse_report(text, "https://github.com/example/demo")
        assert isinstance(report, Report)
        assert len(report.violations) == 1
        assert report.violations[0].rule_id == "SEC-001"
        assert report.violations[0].severity == "critical"

    def test_parse_json_in_markdown_fence(self) -> None:
        from agent_loop import _parse_report

        text = '```json\n{"repository": "x", "violations": []}\n```'
        report = _parse_report(text, "x")
        assert report.violations == []

    def test_parse_empty_violations(self) -> None:
        from agent_loop import _parse_report

        text = '{"repository": "x", "violations": []}'
        report = _parse_report(text, "x")
        assert report.violations == []

    def test_parse_invalid_json_raises(self) -> None:
        from agent_loop import _parse_report

        with pytest.raises(ValueError):
            _parse_report("this is not json at all", "x")

    def test_skips_invalid_severity(self) -> None:
        from agent_loop import _parse_report

        text = json.dumps({
            "repository": "x",
            "violations": [
                {
                    "rule_id": "X-001",
                    "category": "test",
                    "severity": "INVALID_SEVERITY",
                    "description": "d",
                    "evidence": "e",
                    "recommendation": "r",
                }
            ],
        })
        report = _parse_report(text, "x")
        # Should skip the invalid violation
        assert len(report.violations) == 0


class TestToolExecution:
    """Test the _execute_tool helper."""

    def test_unknown_tool(self) -> None:
        from agent_loop import _execute_tool

        result = _execute_tool("nonexistent_tool", {}, "/tmp/fake")
        assert result.error is not None
        assert "Unknown tool" in result.error

    def test_known_tool_list_files(self, tmp_path) -> None:
        from agent_loop import _execute_tool
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "hello.txt").write_text("world")
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)

        result = _execute_tool("list_files", {}, str(repo))
        assert result.error is None
        assert "hello.txt" in result.output


class TestReportModel:
    """Test Report and Violation models."""

    def test_report_to_json(self) -> None:
        r = Report(
            repository="https://github.com/test/repo",
            violations=[
                Violation(
                    rule_id="HYGIENE-001",
                    category="repository_hygiene",
                    severity="medium",
                    description="Missing README",
                    evidence="No README.md found",
                    recommendation="Add a README.md",
                )
            ],
        )
        j = json.loads(r.to_json())
        assert j["repository"] == "https://github.com/test/repo"
        assert len(j["violations"]) == 1
        assert j["violations"][0]["file"] is None
        assert j["violations"][0]["line"] is None

    def test_violation_invalid_severity(self) -> None:
        with pytest.raises(ValueError):
            Violation(
                rule_id="X-001",
                category="test",
                severity="banana",
                description="d",
                evidence="e",
                recommendation="r",
            )
