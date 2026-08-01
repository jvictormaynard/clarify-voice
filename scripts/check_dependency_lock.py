#!/usr/bin/env python3
"""Fail when pip-tools would generate a different dependency lock."""

from __future__ import annotations

import difflib
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-file", default="requirements-lock-linux.txt")
    args = parser.parse_args(argv)
    lockfile = ROOT / args.lock_file
    header = (
        "#    pip-compile --output-file="
        f"{Path(args.lock_file).name} --strip-extras requirements-dev.txt"
    )
    with tempfile.TemporaryDirectory(prefix="clarify-voice-lock-") as directory:
        generated = Path(directory) / Path(args.lock_file).name
        # Seed the output with the committed pins. Without this, pip-tools may
        # select newer versions allowed by the intent ranges and make an old
        # commit fail its own drift check without any requirements change.
        shutil.copyfile(lockfile, generated)
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
            r"(?m)^#    pip-compile --output-file=.*$", header, actual, count=1
        )
        expected = lockfile.read_text(encoding="utf-8")
        if actual != expected:
            print(f"{args.lock_file} is out of date:", file=sys.stderr)
            print(
                "".join(
                    difflib.unified_diff(
                        expected.splitlines(keepends=True),
                        actual.splitlines(keepends=True),
                        fromfile=str(lockfile),
                        tofile="generated lock",
                    )
                ),
                file=sys.stderr,
            )
            return 1

    print(f"{args.lock_file} is current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
