# ClarifyVoice release contract

## Canonical state

- Repository: `jvictormaynard/clarify-voice`
- Release branch base: `master`
- Versioning: Semantic Versioning with `v`-prefixed Git tags
- Maintained application: Python `app.py`
- Historical code: `legacy/electron-prototype/` is not packaged
- Public platform: Windows 10/11, x64 portable executable

The tag, release, and executable must all originate from the same green
`master` commit.

## Required repository files

- `CHANGELOG.md`
- `README.md`
- `docs/README.pt-BR.md`
- `docs/architecture.md`
- `docs/development.md`
- `LICENSE`
- `THIRD_PARTY_NOTICES.md`
- `requirements.txt`
- `requirements-dev.txt`
- `requirements-lock-linux.txt`
- `requirements-lock-windows.txt`
- `requirements-lock-runtime-windows.txt`
- `scripts/check_runtime_lock.py`
- `scripts/install_bootstrap_tools.py`
- `scripts/add_sbom_component.py`
- `scripts/sox-runtime-manifest.json`
- `scripts/build.ps1`
- `scripts/build-installer.ps1`
- `scripts/test-installer.ps1`
- `scripts/create_release_manifest.py`
- `scripts/verify-signature.ps1`
- `distribution/update-policy.json`
- `docs/windows-distribution.md`
- `.github/workflows/ci.yml`
- `.github/workflows/release.yml`

## Required validation

Before the release-preparation PR:

- `git diff --check`
- `npm run check`
- `npm test`
- release preflight script

For Windows-facing changes:

- `npm run deploy`
- installed process path and responsiveness check
- manual acceptance of visible behavior and relevant hotkeys

On the PR and after merge:

- `Tests (ubuntu-latest)`
- `Tests (windows-latest)`
- `Package Windows executable`
  - includes per-user MSI install, upgrade, repair, rollback, uninstall, and
    signed-manifest contract smoke tests

On the tag:

- successful `Release` workflow
- Azure OIDC login and Artifact Signing actions pinned to reviewed full commit
  SHAs; mutable tags are not an acceptable release trust boundary

## Required release assets

The GitHub release must contain exactly one of each:

1. `ClarifyVoice.exe`
2. `ClarifyVoice.exe.sha256`
3. `ClarifyVoice.sbom.json`
4. `ClarifyVoice-windows-x64.msi`
5. `ClarifyVoice-windows-x64.msi.sha256`
6. `ClarifyVoice-release-manifest.cab`
7. `ClarifyVoice-release-manifest.cab.sha256`
8. `ClarifyVoice-windows-x64.zip`
9. `sox-14.4.2-source.tar.gz`

The ZIP must contain:

- `ClarifyVoice.exe`
- `ClarifyVoice.sbom.json`
- `LICENSE`
- `THIRD_PARTY_NOTICES.md`

The SoX source archive must match:

`b45f598643ffbd8e363ff24d61166ccec4836fea6d3888881b8df53e3bb55f6c`

## Documentation ownership

- `CHANGELOG.md`: released user-visible behavior and comparison links
- `README.md`: primary English install/download path
- `docs/README.pt-BR.md`: equivalent Portuguese onboarding
- `docs/development.md`: maintainer setup, validation, tagging, and release flow
- `docs/architecture.md`: packaging and runtime boundaries
- `THIRD_PARTY_NOTICES.md`: bundled third-party components and notices

Only update a document when its owned contract changed. Keep English and
Portuguese installation instructions behaviorally equivalent.

## Security and provenance

- Never bundle `.env`, API keys, `%APPDATA%` config, or local credentials.
- Keep provider credentials in `%APPDATA%\ClarifyVoice\config.json`.
- Preserve contributor attribution and existing Git history.
- Never force-update or reuse a published tag.
- Never overwrite a published asset to conceal provenance drift; publish a new
  patch version instead.
- Require valid, timestamped Authenticode signatures from the publisher pinned
  in `distribution/update-policy.json` for EXE, MSI, and manifest CAB.
- Require GitHub build-provenance attestations for EXE, MSI, CAB, and ZIP.
- Keep the unsigned v0.1.2-and-earlier SmartScreen limitation explicit.
- Follow `docs/windows-distribution.md` for signing ownership, cost, rotation,
  revocation, rollout prerequisites, and manual acceptance.
