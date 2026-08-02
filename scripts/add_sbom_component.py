#!/usr/bin/env python3
"""Add the verified bundled SoX runtime to a generated CycloneDX BOM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SOX_COMPONENT = {
    "bom-ref": "pkg:generic/sox@14.4.2",
    "description": "Bundled SoX Windows runtime and codecs.",
    "externalReferences": [
        {
            "comment": "Verified source archive used for the release.",
            "type": "distribution",
            "url": "https://downloads.sourceforge.net/project/sox/sox/14.4.2/sox-14.4.2.tar.gz",
        }
    ],
    "hashes": [
        {
            "alg": "SHA-256",
            "content": "b45f598643ffbd8e363ff24d61166ccec4836fea6d3888881b8df53e3bb55f6c",
        }
    ],
    "licenses": [{"license": {"id": "GPL-2.0-or-later"}}],
    "name": "SoX",
    "purl": "pkg:generic/sox@14.4.2",
    "type": "application",
    "version": "14.4.2",
}


def add_component(path: Path) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to read CycloneDX BOM: {error}") from error
    components = document.get("components")
    if not isinstance(components, list):
        raise ValueError("CycloneDX BOM must contain a components list")
    if any(
        isinstance(component, dict)
        and component.get("bom-ref") == SOX_COMPONENT["bom-ref"]
        for component in components
    ):
        raise ValueError("CycloneDX BOM already contains the bundled SoX component")
    components.append(SOX_COMPONENT)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bom-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        add_component(args.bom_file)
    except ValueError as error:
        print(str(error))
        return 1
    print(f"Added bundled SoX 14.4.2 to {args.bom_file}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
