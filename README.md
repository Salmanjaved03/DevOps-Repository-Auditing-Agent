# DevOps Repository Auditing Agent

An AI-powered agent that autonomously inspects public GitHub repositories for DevOps best-practice violations and generates an evidence-based JSON report with actionable recommendations.

Developed for **MLOps & Production AI Engineering — Assignment 2**.

---

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────┐
│  agent.py   │────▶│ agent_loop.py│────▶│  llm.py  │
│  (CLI)      │     │ (Tool Loop)  │     │  (Groq)  │
└─────────────┘     └──────┬───────┘     └──────────┘
                           │
                    ┌──────▼───────┐
                    │   tools.py   │
                    │ (Inspection) │
                    └──────────────┘
```

### Agent Loop (Observe → Reason → Act → Report)

1. **Clone** the target repository into a temporary directory (`--depth=50`)
2. **Initialize** the LLM with a DevOps auditing system prompt and tool definitions
3. **Loop**:
   - LLM decides which inspection tool(s) to call based on current observations
   - Agent executes the tool(s) on the cloned repo and returns output to the LLM
   - LLM analyzes findings and decides follow-up investigations
4. **Report** — LLM produces a structured JSON report conforming to the assignment schema
5. **Cleanup** — target repository is cleaned up safely from the temp directory

The LLM drives the investigation dynamically — it is **not** a fixed checklist or static scanner.

### Framework Restriction Compliance
Per assignment requirements, prebuilt agent frameworks (**LangChain**, **LangGraph**, **n8n**, etc.) are **prohibited**. The agent loop, tool dispatch, message trimming, and control flow are implemented purely in Python using the standard provider client SDK.

### Inspection Tools

| Tool | Signature | Purpose |
|------|-----------|---------|
| `list_files` | `(repo_dir) -> str` | Discover repository file structure and key configs |
| `read_file` | `(repo_dir, path) -> str` | Read specific files with line numbers (max ~12 KB) |
| `search_code` | `(repo_dir, pattern) -> str` | Regex search across repository files |
| `git_log` | `(repo_dir, max_commits=15) -> str` | Inspect recent commit history |
| `check_github_actions` | `(repo_dir) -> str` | Inspect workflow files in `.github/workflows/` |
| `list_branches` | `(repo_dir) -> str` | List local and remote branches |
| `check_dependencies` | `(repo_dir) -> str` | Analyze dependencies, pinning, and lockfile presence |

### DevOps Categories Inspected

1. **repository_hygiene** — Missing README, inadequate .gitignore, tracked temp/cache files
2. **dependency_management** — Unpinned dependencies, missing lockfiles, inconsistent specs
3. **code_quality** — Missing linter/formatter configs (ruff, flake8, black, etc.)
4. **testing** — Missing test suites, missing test configuration, no CI test runs
5. **ci_cd** — Ineffective CI gates, missing PR triggers, skipped build/test validation
6. **security** — Hardcoded credentials, exposed secrets, dangerous permissions
7. **configuration** — Committed local env files (.env), machine-specific absolute paths
8. **collaboration** — Unsafe PR automation, missing review guidelines where needed
9. **build_release** — Missing build verification, unpinned action versions, risky release steps

---

## Installation

### Prerequisites

- Python 3.10+
- Git installed and on PATH
- A free [Groq API key](https://console.groq.com)

### Setup

```bash
# Clone this repository
git clone <repository_url>
cd <repository_folder>

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and paste your GROQ_API_KEY
```

---

## Usage

```bash
python agent.py --repo <github_repository_url> --output report.json
```

### Examples

```bash
# Audit Flask and save output
python agent.py --repo https://github.com/pallets/flask --output report.json

# Audit with verbose debug logs
python agent.py --repo https://github.com/pallets/flask --output report.json --verbose
```

### Command-line Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--repo` | Yes | Public GitHub repository URL |
| `--output` | No | Output JSON file path (default: `report.json`) |
| `--verbose` / `-v` | No | Enable debug logging |

---

## Output Format

The agent outputs valid JSON matching the exact contract:

```json
{
  "repository": "https://github.com/example/demo-project",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "Hardcoded API credential",
      "evidence": "Credential assigned directly in source code",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```

- Allowed severities: `critical`, `high`, `medium`, `low`
- Inapplicable file/line fields are set to `null`
- Never reproduces raw secret values in output
- If no violations are detected, returns an empty `"violations": []` array

---

## Automated Tests

Run the test suite with pytest:

```bash
python -m pytest tests/ -v
```

All 24 automated unit and integration tests verify tool functionality, path safety, and report parsing.
