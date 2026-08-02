#!/usr/bin/env python3
"""Ensure runtime pins are identical in the Windows build and runtime locks."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_LOCK = ROOT / "requirements-lock-windows.txt"
RUNTIME_LOCK = ROOT / "requirements-lock-runtime-windows.txt"
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*(\S.*))?$")


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"{path.name}:{line_number}: malformed lock pin")
        name, version, _marker = match.groups()
        pins[name.lower().replace("_", "-")] = version
    return pins


def _shared_pin_mismatches(
    build_lock: Path, runtime_lock: Path
) -> dict[str, tuple[str | None, str]]:
    build = _pins(build_lock)
    runtime = _pins(runtime_lock)
    return {
        name: (build.get(name), version)
        for name, version in sorted(runtime.items())
        if build.get(name) != version
    }


def main() -> int:
    try:
        mismatches = _shared_pin_mismatches(BUILD_LOCK, RUNTIME_LOCK)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    if mismatches:
        print("Runtime/build lock pin mismatch:", file=sys.stderr)
        for name, (build, runtime) in mismatches.items():
            build_version = build or "<missing>"
            print(
                f"- {name}: build={build_version}, runtime={runtime}",
                file=sys.stderr,
            )
        return 1
    print("Shared runtime pins match the Windows build lock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
