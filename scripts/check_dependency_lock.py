#!/usr/bin/env python3
"""Fail when pip-tools would generate a different dependency lock."""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "requirements-lock.txt"
HEADER = "#    pip-compile --output-file=requirements-lock.txt --strip-extras requirements-dev.txt"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="clarify-voice-lock-") as directory:
        generated = Path(directory) / "requirements-lock.txt"
        command = [
            sys.executable,
            "-m",
            "piptools",
            "compile",
            "--strip-extras",
            "--output-file",
            str(generated),
            "requirements-dev.txt",
        ]
        try:
            result = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, timeout=300
            )
        except subprocess.TimeoutExpired:
            print("pip-tools lock generation exceeded five minutes.", file=sys.stderr)
            return 1
        if result.returncode:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            return result.returncode

        actual = generated.read_text(encoding="utf-8")
        actual = re.sub(
            r"(?m)^#    pip-compile --output-file=.*$", HEADER, actual, count=1
        )
        expected = LOCKFILE.read_text(encoding="utf-8")
        if actual != expected:
            print("requirements-lock.txt is out of date:", file=sys.stderr)
            print(
                "".join(
                    difflib.unified_diff(
                        expected.splitlines(keepends=True),
                        actual.splitlines(keepends=True),
                        fromfile=str(LOCKFILE),
                        tofile="generated lock",
                    )
                ),
                file=sys.stderr,
            )
            return 1

    print("requirements-lock.txt is current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
