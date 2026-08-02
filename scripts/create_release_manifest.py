#!/usr/bin/env python3
"""Create the release manifest that will be wrapped in a signed CAB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from update_security import load_update_policy, parse_release_manifest, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()

    policy = load_update_policy(args.policy)
    installer = args.installer.resolve()
    if not installer.is_file() or installer.name != policy.installer_asset:
        raise SystemExit("installer path does not match update policy asset")

    version = args.version.removeprefix("v")
    release_tag = f"v{version}"
    payload = {
        "schema_version": 1,
        "version": version,
        "release_tag": release_tag,
        "channel": policy.channel,
        "asset": {
            "name": policy.installer_asset,
            "url": (
                f"https://github.com/{policy.repository}/releases/download/"
                f"{release_tag}/{policy.installer_asset}"
            ),
            "sha256": sha256_file(installer),
            "size": installer.stat().st_size,
            "publisher_common_name": policy.publisher_common_name,
        },
    }
    encoded = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n").encode("utf-8")
    parse_release_manifest(encoded, policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    print(f"Created {args.output} for {release_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
