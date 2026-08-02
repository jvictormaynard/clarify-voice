#!/usr/bin/env python3
"""Install the pip/setuptools pins recorded in a development lock."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^(pip|setuptools)==([^\s;]+)(?:\s*;\s*.*)?$")


def _pins(lock_file: Path) -> list[str]:
    found: dict[str, str] = {}
    for line in lock_file.read_text(encoding="utf-8").splitlines():
        match = PIN.fullmatch(line.strip())
        if match:
            found[match.group(1)] = match.group(2)
    missing = sorted({"pip", "setuptools"} - found.keys())
    if missing:
        raise ValueError(
            f"{lock_file.name} must pin bootstrap tools: {', '.join(missing)}"
        )
    return [f"{name}=={found[name]}" for name in ("pip", "setuptools")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-file", required=True)
    args = parser.parse_args(argv)
    lock_file = ROOT / args.lock_file
    try:
        pins = _pins(lock_file)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *pins],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
