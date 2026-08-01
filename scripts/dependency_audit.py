#!/usr/bin/env python3
"""Run pip-audit and enforce the repository's reviewed-exception policy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "requirements-lock.txt"
POLICYFILE = ROOT / "dependency-audit.json"


def _load_policy() -> dict[str, str]:
    policy = json.loads(POLICYFILE.read_text(encoding="utf-8"))
    if policy.get("policy_version") != 1:
        raise ValueError("unsupported dependency-audit.json policy version")
    exceptions = policy.get("ignored_vulnerabilities", {})
    if not isinstance(exceptions, dict):
        raise ValueError("ignored_vulnerabilities must be an object")
    if any(
        not isinstance(reason, str) or not reason.strip()
        for reason in exceptions.values()
    ):
        raise ValueError("every ignored vulnerability needs a non-empty reason")
    return {str(vulnerability): reason for vulnerability, reason in exceptions.items()}


def main() -> int:
    exceptions = _load_policy()
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--format",
        "json",
        "--progress-spinner",
        "off",
        "--timeout",
        "30",
        "--requirement",
        str(LOCKFILE),
    ]
    try:
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        print("pip-audit exceeded the five-minute network timeout.", file=sys.stderr)
        return 1
    if result.stdout.strip():
        try:
            findings = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            print(result.stdout, end="")
            print(f"pip-audit returned invalid JSON: {error}", file=sys.stderr)
            return result.returncode or 1
    else:
        if result.returncode:
            print(result.stderr, end="", file=sys.stderr)
            return result.returncode
        findings = []

    vulnerabilities = {
        vulnerability["id"]
        for package in findings
        for vulnerability in package.get("vulns", [])
    }
    unreviewed = sorted(vulnerabilities - exceptions.keys())
    if unreviewed:
        print("Unreviewed dependency vulnerabilities:", file=sys.stderr)
        for vulnerability in unreviewed:
            print(f"- {vulnerability}", file=sys.stderr)
        print(
            "Add a reviewed rationale to dependency-audit.json only after "
            "maintainer approval.",
            file=sys.stderr,
        )
        return 1

    if vulnerabilities:
        print("Dependency vulnerabilities covered by reviewed policy:")
        for vulnerability in sorted(vulnerabilities):
            print(f"- {vulnerability}: {exceptions[vulnerability]}")
    else:
        print("No dependency vulnerabilities found in requirements-lock.txt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
