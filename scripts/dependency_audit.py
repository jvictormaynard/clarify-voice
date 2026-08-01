#!/usr/bin/env python3
"""Run pip-audit and enforce the repository's reviewed-exception policy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / (
    "requirements-lock-windows.txt"
    if sys.platform == "win32"
    else "requirements-lock-linux.txt"
)
POLICYFILE = ROOT / "dependency-audit.json"


def _vulnerability_ids(payload: object) -> set[str]:
    """Extract vulnerability IDs from pip-audit's object or legacy list JSON."""
    if isinstance(payload, dict):
        findings = payload.get("dependencies", [])
    elif isinstance(payload, list):
        findings = payload
    else:
        raise ValueError("pip-audit returned an unexpected JSON shape")
    if not isinstance(findings, list):
        raise ValueError("pip-audit dependencies must be a list")

    vulnerabilities: set[str] = set()
    for package in findings:
        if not isinstance(package, dict):
            raise ValueError("pip-audit package entries must be objects")
        package_vulnerabilities = package.get("vulns", [])
        if not isinstance(package_vulnerabilities, list):
            raise ValueError("pip-audit package vulnerabilities must be a list")
        for vulnerability in package_vulnerabilities:
            if not isinstance(vulnerability, dict) or not isinstance(
                vulnerability.get("id"), str
            ):
                raise ValueError("pip-audit vulnerability entries need an id")
            vulnerabilities.add(vulnerability["id"])
    return vulnerabilities


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
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            print(result.stdout, end="")
            print(f"pip-audit returned invalid JSON: {error}", file=sys.stderr)
            return result.returncode or 1
    else:
        if result.returncode:
            print(result.stderr, end="", file=sys.stderr)
            return result.returncode

    try:
        vulnerabilities = _vulnerability_ids(payload if result.stdout.strip() else [])
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
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
        print(f"No dependency vulnerabilities found in {LOCKFILE.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
