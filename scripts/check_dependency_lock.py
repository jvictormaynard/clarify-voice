#!/usr/bin/env python3
"""Fail when pip-tools would generate a different dependency lock."""

from __future__ import annotations

import argparse
import difflib
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
    parser.add_argument("--requirements-file", default="requirements-dev.txt")
    parser.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="include pip and setuptools in a development lock",
    )
    args = parser.parse_args(argv)
    lockfile = ROOT / args.lock_file
    allow_unsafe = "--allow-unsafe " if args.allow_unsafe else ""
    unsafe_arguments = ["--allow-unsafe"] if args.allow_unsafe else []
    header = (
        f"#    pip-compile {allow_unsafe}--output-file="
        f"{Path(args.lock_file).name} --strip-extras {args.requirements_file}"
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
            *unsafe_arguments,
            "--strip-extras",
            "--output-file",
            str(generated),
            args.requirements_file,
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
            r"(?m)^#    pip-compile (?:--allow-unsafe )?--output-file=.*$",
            header,
            actual,
            count=1,
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
