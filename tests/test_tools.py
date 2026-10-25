"""Unit tests for inspection tools (tools.py)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from tools import (
    list_files,
    read_file,
    search_code,
    git_log,
    check_github_actions,
    list_branches,
    check_dependencies,
)


# ---------------------------------------------------------------------------
# Fixture: tiny git repo with known content
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_repo(tmp_path: Path) -> str:
    """Create a tiny git repo with a few files for testing."""
    repo = tmp_path / "sample_repo"
    repo.mkdir()

    # Initialise git repo
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=str(repo), capture_output=True, check=True,
    )

    # Create files
    (repo / "README.md").write_text("# Sample\nA test repo.\n")
    (repo / "app.py").write_text(
        textwrap.dedent("""\
            import os
            API_KEY = "sk-secret-12345"
            def main():
                print("hello")
        """)
    )
    (repo / "requirements.txt").write_text("flask\nrequests>=2.0\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")

    # GitHub Actions workflow
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_text(
        textwrap.dedent("""\
            name: CI
            on: [push]
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: echo "hello"
        """)
    )

    # Commit
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo), capture_output=True, check=True,
    )

    return str(repo)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_lists_known_files(self, sample_repo: str) -> None:
        output = list_files(sample_repo)
        assert "README.md" in output
        assert "app.py" in output
        assert "requirements.txt" in output
        assert ".gitignore" in output

    def test_excludes_dot_git(self, sample_repo: str) -> None:
        output = list_files(sample_repo)
        assert ".git" not in output or ".github" in output
        # More precise: lines should not start with .git/
        for line in output.splitlines():
            assert not line.startswith(".git\\") and not line.startswith(".git/")

    def test_empty_repo(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        subprocess.run(["git", "init"], cwd=str(empty), capture_output=True, check=True)
        output = list_files(str(empty))
        assert output == "(empty repository)"


class TestReadFile:
    def test_reads_known_file(self, sample_repo: str) -> None:
        output = read_file(sample_repo, "README.md")
        assert "Sample" in output
        # Should have line numbers
        assert "1 |" in output or "   1 |" in output

    def test_missing_file(self, sample_repo: str) -> None:
        output = read_file(sample_repo, "nonexistent.txt")
        assert "[error]" in output

    def test_path_traversal_blocked(self, sample_repo: str) -> None:
        output = read_file(sample_repo, "../../etc/passwd")
        assert "[error]" in output


class TestSearchCode:
    def test_finds_pattern(self, sample_repo: str) -> None:
        output = search_code(sample_repo, "API_KEY")
        assert "app.py" in output
        assert "sk-secret" in output

    def test_no_match(self, sample_repo: str) -> None:
        output = search_code(sample_repo, "XYZNONEXISTENT")
        assert "No matches" in output

    def test_invalid_regex(self, sample_repo: str) -> None:
        output = search_code(sample_repo, "[invalid")
        assert "[error]" in output


class TestGitLog:
    def test_returns_commits(self, sample_repo: str) -> None:
        output = git_log(sample_repo)
        assert "Initial commit" in output


class TestCheckGithubActions:
    def test_finds_workflows(self, sample_repo: str) -> None:
        output = check_github_actions(sample_repo)
        assert "ci.yml" in output
        assert "actions/checkout" in output

    def test_no_workflows_dir(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        output = check_github_actions(str(bare))
        assert "No .github/workflows" in output


class TestListBranches:
    def test_lists_branches(self, sample_repo: str) -> None:
        output = list_branches(sample_repo)
        # Should contain main or master
        assert "main" in output or "master" in output


class TestCheckDependencies:
    def test_finds_requirements(self, sample_repo: str) -> None:
        output = check_dependencies(sample_repo)
        assert "requirements.txt" in output

    def test_reports_unpinned(self, sample_repo: str) -> None:
        output = check_dependencies(sample_repo)
        # 'flask' has no version pin
        assert "flask" in output.lower() or "Unpinned" in output
