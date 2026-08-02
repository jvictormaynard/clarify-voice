#!/usr/bin/env python3
"""Add the verified bundled SoX runtime to a generated CycloneDX BOM."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SOURCE_ARCHIVE_SHA256 = (
    "b45f598643ffbd8e363ff24d61166ccec4836fea6d3888881b8df53e3bb55f6c"
)
SOX_COMPONENT_BASE = {
    "bom-ref": "pkg:generic/sox@14.4.2",
    "description": "Bundled SoX Windows runtime and codecs; file hashes below are for the packaged bytes.",
    "externalReferences": [
        {
            "comment": f"Source offer verified with SHA-256 {SOURCE_ARCHIVE_SHA256}.",
            "type": "distribution",
            "url": "https://downloads.sourceforge.net/project/sox/sox/14.4.2/sox-14.4.2.tar.gz",
        }
    ],
    "licenses": [{"license": {"id": "GPL-2.0-or-later"}}],
    "name": "SoX",
    "purl": "pkg:generic/sox@14.4.2",
    "type": "application",
    "version": "14.4.2",
}


def runtime_files(runtime_root: Path, manifest_path: Path) -> list[Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_glob = manifest.get("runtime_glob")
    explicit_files = manifest.get("runtime_files")
    if (
        not isinstance(runtime_glob, str)
        or not isinstance(explicit_files, list)
        or any(not isinstance(name, str) for name in explicit_files)
    ):
        raise ValueError("SoX runtime manifest has an invalid file selection")
    paths = sorted(runtime_root.glob(runtime_glob))
    paths.extend(runtime_root / name for name in explicit_files)
    if not paths or any(not path.is_file() for path in paths):
        raise ValueError("SoX runtime manifest selected missing files")
    if len({path.name.lower() for path in paths}) != len(paths):
        raise ValueError("SoX runtime manifest selected duplicate files")
    return sorted(paths, key=lambda path: path.name.lower())


def add_component(path: Path, runtime_root: Path, manifest_path: Path) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to read CycloneDX BOM: {error}") from error
    components = document.get("components")
    if not isinstance(components, list):
        raise ValueError("CycloneDX BOM must contain a components list")
    if any(
        isinstance(component, dict)
        and component.get("bom-ref") == SOX_COMPONENT_BASE["bom-ref"]
        for component in components
    ):
        raise ValueError("CycloneDX BOM already contains the bundled SoX component")
    file_properties = []
    for runtime_file in runtime_files(runtime_root, manifest_path):
        digest = hashlib.sha256(runtime_file.read_bytes()).hexdigest()
        file_properties.append(
            {
                "name": f"clarifyvoice:bundled-runtime-file:{runtime_file.name}:sha-256",
                "value": digest,
            }
        )
    component = {**SOX_COMPONENT_BASE, "properties": file_properties}
    components.append(component)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bom-file", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        add_component(args.bom_file, args.runtime_root, args.manifest)
    except ValueError as error:
        print(str(error))
        return 1
    print(f"Added bundled SoX 14.4.2 to {args.bom_file}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
