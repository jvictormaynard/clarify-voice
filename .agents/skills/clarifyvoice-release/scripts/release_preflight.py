#!/usr/bin/env python3
"""Read-only preflight checks for a ClarifyVoice release candidate."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


REQUIRED_FILES = (
    "CHANGELOG.md",
    "README.md",
    "docs/README.pt-BR.md",
    "docs/architecture.md",
    "docs/development.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-lock-linux.txt",
    "requirements-lock-windows.txt",
    "requirements-lock-runtime-windows.txt",
    "scripts/check_runtime_lock.py",
    "scripts/install_bootstrap_tools.py",
    "scripts/add_sbom_component.py",
    "scripts/sox-runtime-manifest.json",
    "version.py",
    "scripts/build.ps1",
    "scripts/build-installer.ps1",
    "scripts/test-installer.ps1",
    "scripts/create_release_manifest.py",
    "scripts/verify-signature.ps1",
    "distribution/update-policy.json",
    "docs/windows-distribution.md",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
)

REQUIRED_ASSETS = (
    "ClarifyVoice.exe",
    "ClarifyVoice.exe.sha256",
    "ClarifyVoice.sbom.json",
    "ClarifyVoice-windows-x64.msi",
    "ClarifyVoice-windows-x64.msi.sha256",
    "ClarifyVoice-release-manifest.cab",
    "ClarifyVoice-release-manifest.cab.sha256",
    "ClarifyVoice-windows-x64.zip",
    "sox-14.4.2-source.tar.gz",
)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def parse_version(raw: str) -> tuple[str, tuple[int, int, int]]:
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", raw)
    if not match:
        raise ValueError("version must be SemVer X.Y.Z, optionally prefixed by v")
    number = ".".join(match.groups())
    return number, tuple(int(part) for part in match.groups())


def version_tags(repo: Path) -> list[tuple[tuple[int, int, int], str]]:
    tags = git(repo, "tag", "--list", "v*").splitlines()
    parsed = []
    for tag in tags:
        try:
            _, parts = parse_version(tag)
        except ValueError:
            continue
        parsed.append((parts, tag))
    return sorted(parsed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()

    repo = args.repo.resolve()
    version, version_parts = parse_version(args.version)
    tag = f"v{version}"
    failures: list[str] = []

    if not (repo / ".git").exists():
        failures.append(f"not a Git repository: {repo}")

    missing = [path for path in REQUIRED_FILES if not (repo / path).is_file()]
    if missing:
        failures.append("missing required files: " + ", ".join(missing))

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    version_source = (repo / "version.py").read_text(encoding="utf-8")
    version_match = re.search(
        r'^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"$',
        version_source,
        re.MULTILINE,
    )
    if not version_match or version_match.group(1) != version:
        failures.append("version.py does not match the proposed release version")

    tags = version_tags(repo)
    existing_tags = {existing_tag for _, existing_tag in tags}
    if tag in existing_tags:
        failures.append(f"tag already exists: {tag}")

    previous = [item for item in tags if item[0] < version_parts]
    if not previous:
        failures.append("no previous SemVer tag found")
        previous_tag = None
    else:
        previous_tag = previous[-1][1]

    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    heading_pattern = rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$"
    if not re.search(heading_pattern, changelog, re.MULTILINE):
        failures.append(f"CHANGELOG.md has no dated [{version}] section")

    owner_url = "https://github.com/jvictormaynard/clarify-voice"
    expected_unreleased = f"[Unreleased]: {owner_url}/compare/{tag}...HEAD"
    if expected_unreleased not in changelog:
        failures.append("Unreleased comparison does not start at the new tag")

    if previous_tag:
        expected_version = (
            f"[{version}]: {owner_url}/compare/{previous_tag}...{tag}"
        )
        if expected_version not in changelog:
            failures.append("version comparison link is missing or incorrect")

    release_workflow = (
        repo / ".github/workflows/release.yml"
    ).read_text(encoding="utf-8")
    if not re.search(r'^\s*-\s*["\']v\*["\']\s*$', release_workflow, re.MULTILINE):
        failures.append("release workflow is not triggered by v* tags")
    for asset in REQUIRED_ASSETS:
        if asset not in release_workflow:
            failures.append(f"release workflow does not reference {asset}")
    if "requirements-lock-windows.txt" not in release_workflow:
        failures.append("release workflow does not use requirements-lock-windows.txt")
    if "requirements-lock-runtime-windows.txt" not in release_workflow:
        failures.append(
            "release workflow does not use requirements-lock-runtime-windows.txt"
        )
    if (
        "cyclonedx-py requirements requirements-lock-runtime-windows.txt"
        not in release_workflow
    ):
        failures.append("release SBOM is not generated from the runtime lock")
    if "attest-build-provenance" not in release_workflow:
        failures.append("release workflow does not publish artifact provenance")
    for required_gate in (
        "azure/artifact-signing-action@v2",
        "verify-signature.ps1",
        "actions/attest-build-provenance@",
    ):
        if required_gate not in release_workflow:
            failures.append(f"release workflow is missing gate: {required_gate}")

    readme = (repo / "README.md").read_text(encoding="utf-8")
    readme_pt = (repo / "docs/README.pt-BR.md").read_text(encoding="utf-8")
    latest_url = f"{owner_url}/releases/latest"
    if latest_url not in readme or latest_url not in readme_pt:
        failures.append("English and Portuguese READMEs must link to releases/latest")

    if args.require_clean and git(repo, "status", "--porcelain"):
        failures.append("worktree is not clean")

    current_branch = git(repo, "branch", "--show-current")
    head = git(repo, "rev-parse", "--short=12", "HEAD")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print(f"OK: ClarifyVoice {tag} release preflight passed")
    print(f"Repository: {repo}")
    print(f"Branch: {current_branch}")
    print(f"HEAD: {head}")
    print(f"Previous tag: {previous_tag}")
    print(f"Checked on: {date.today().isoformat()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
