#!/usr/bin/env python3
"""CLI entry point for the DevOps Repository Auditing Agent.

Usage:
    python agent.py --repo <github_repository_url> --output report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

from dotenv import load_dotenv


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _validate_github_url(url: str) -> str:
    """Basic validation that *url* looks like a GitHub repo URL."""
    pattern = r"^https?://github\.com/[\w.\-]+/[\w.\-]+(\.git)?/?$"
    if not re.match(pattern, url):
        raise argparse.ArgumentTypeError(
            f"Invalid GitHub repository URL: {url}\n"
            "Expected format: https://github.com/<owner>/<repo>"
        )
    # Normalise: strip trailing .git and /
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AI-powered DevOps repository auditing agent.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        type=_validate_github_url,
        help="Public GitHub repository URL to audit.",
    )
    parser.add_argument(
        "--output",
        default="report.json",
        help="Path to write the JSON audit report (default: report.json).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging.",
    )

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    logger = logging.getLogger("agent")

    # Load environment variables
    load_dotenv()

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error(
            "GROQ_API_KEY not set. Copy .env.example to .env and add your key."
        )
        return 1

    logger.info("Starting audit of %s", args.repo)

    try:
        # Import here so missing deps fail early with a clear message
        from agent_loop import run_agent

        report = run_agent(args.repo, api_key=api_key)
    except Exception as exc:
        logger.error("Fatal error during audit: %s", exc, exc_info=True)
        return 1

    # Write report
    try:
        report_json = report.to_json(indent=2)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report_json + "\n")
        logger.info("Report written to %s", args.output)
        logger.info(
            "Found %d violation(s).", len(report.violations)
        )
    except OSError as exc:
        logger.error("Could not write report: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
