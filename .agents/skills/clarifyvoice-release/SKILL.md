---
name: clarifyvoice-release
description: Prepare, publish, and verify ClarifyVoice GitHub releases with SemVer, changelog and documentation maintenance, Windows executable validation, CI gates, tags, release assets, checksums, and post-release checks. Use when the user asks to release, publish, tag, version, or audit a ClarifyVoice release in the clarify-voice repository.
---

# ClarifyVoice Release

Publish a reproducible Windows-first ClarifyVoice release without allowing the
tag, documentation, executable, or source state to drift apart.

## Required context

Operate on `/home/ork/repos/clarify-voice` unless the user supplies another
ClarifyVoice checkout. Read [references/release-contract.md](references/release-contract.md)
completely before changing release state.

Use local `git` for branches, commits, tags, and pushes. Prefer the GitHub
connector for PR creation and metadata; use `gh` for authentication, Actions,
release inspection, downloads, and gaps in connector coverage.

## Workflow

### 1. Establish the release boundary

1. Fetch and prune `origin`, including tags.
2. Require an understood, clean worktree before creating a release branch.
3. Inspect the latest GitHub release, existing tags, open PRs, and commits since
   the latest tag.
4. Choose the next SemVer:
   - patch for compatible fixes and dependency maintenance;
   - minor for backward-compatible user features;
   - major only for intentional breaking changes.
5. Stop if the proposed tag already exists or the target commits are not on the
   canonical `master`.

### 2. Prepare release documentation

Create `agent/release-vX.Y.Z` from current `origin/master`.

Update `CHANGELOG.md`:

- move user-visible entries from `Unreleased` into `[X.Y.Z] - YYYY-MM-DD`;
- group entries under `Added`, `Changed`, `Fixed`, `Security`, or `Removed`;
- describe observable behavior, not commit mechanics;
- change the `Unreleased` comparison to start at the new tag;
- add the comparison link from the previous tag to the new tag.

Set `__version__` in `version.py` and the top-level `version` in `package.json`
to the same exact `X.Y.Z` value before running preflight. The tag, manifest,
MSI, in-app comparison, runtime diagnostics, and packaged executable derive
from this value; the preflight rejects drift between the module, package
metadata, and proposed tag.

Audit documentation touched by the release contract:

- update `README.md` and `docs/README.pt-BR.md` when installation, shortcuts,
  configuration, requirements, or download behavior changed;
- update architecture/development docs when build or maintainer workflows changed;
- update `THIRD_PARTY_NOTICES.md` when bundled components or their terms changed;
- keep secrets, `.env`, local config, and credentials out of every artifact.

Do not make cosmetic documentation edits merely to create a release commit.

### 3. Run preflight and product validation

Run:

```bash
python3 .agents/skills/clarifyvoice-release/scripts/release_preflight.py \
  --repo . \
  --version X.Y.Z
git diff --check
npm run check
npm test
```

For Windows UI, dependency, packaging, hotkey, focus, microphone, or
transparency changes, also run `npm run deploy`. Confirm that
`C:\repos\clarify-voice\dist\ClarifyVoice.exe` restarted and is responding.
Require the user's manual acceptance for visible or interaction changes.

Never treat unit tests or a successful PyInstaller build as visual acceptance.

### 4. Publish the release-preparation PR

1. Stage only release-preparation files.
2. Commit tersely, for example `docs: prepare vX.Y.Z release`.
3. Push the branch with upstream tracking.
4. Open a draft PR against `master` explaining scope, version choice,
   documentation changes, validation, and expected assets.
5. Mark ready only after local gates pass.
6. Require all PR checks:
   - `Tests (ubuntu-latest)`;
  - `Tests (windows-latest)`;
  - `Package Windows executable` (including MSI lifecycle and manifest smoke
    tests).
7. Merge without rewriting existing contributor history.
8. Wait for the post-merge `master` CI, including Windows packaging.

Do not tag an unmerged branch or a commit whose post-merge CI is failing.

### 5. Tag and publish

Resolve the exact green `origin/master` SHA. Create an annotated tag:

```bash
git tag -a vX.Y.Z <master-sha> -m "ClarifyVoice vX.Y.Z"
git push origin vX.Y.Z
```

Watch the tag-triggered `Release` workflow. It must run tests, build on Windows,
sign and verify the EXE, MSI, and manifest CAB through the protected Azure OIDC
environment, create checksums, ZIP, and provenance attestations, obtain the
verified SoX source archive, and publish the GitHub release.

Do not create a second manual release while the workflow is running.

### 6. Verify the published release

Require all of the following:

- release is published, not draft or prerelease;
- tag resolves to the intended green `master` commit;
- release is the current latest release;
- all required assets from the contract exist exactly once;
- downloaded `ClarifyVoice.exe` matches `ClarifyVoice.exe.sha256`;
- downloaded MSI and manifest CAB match their checksums, have valid RFC 3161
  timestamped Authenticode signatures, and match the pinned publisher;
- authenticated manifest version, tag, channel, asset name, URL, size, and MSI
  checksum all match the release;
- GitHub provenance attestations verify for the EXE, MSI, CAB, and ZIP;
- ZIP contains the executable, `LICENSE`, and `THIRD_PARTY_NOTICES.md`;
- SoX source digest matches the pinned release-workflow digest;
- `/releases/latest` resolves to the new version.

Download assets into a temporary directory for verification. Do not replace the
user's installed executable with a release download unless explicitly asked.

### 7. Hand off

Report:

- version, tag, release URL, and commit SHA;
- PR and merge result;
- local, PR, `master`, and release-workflow validation;
- exact published assets and checksum result;
- documentation files updated;
- any remaining signing or SmartScreen limitation.
- manual installer acceptance evidence and any unmet rollout gate from
  `docs/windows-distribution.md`.

Keep the local checkout on clean, synchronized `master`. Prune merged temporary
branches from remote-tracking refs.

## Stop conditions

Stop publication and explain the blocker if:

- unrelated local changes make scope ambiguous;
- a required test, package job, or post-merge check fails;
- Windows interaction changes lack real executable acceptance;
- the tag exists at another commit;
- the release workflow or any required asset is missing;
- signing configuration, timestamp, publisher identity, or provenance fails;
- the installer/update rollout gates are incomplete for a release that intends
  to enable that path;
- checksum, ZIP contents, SoX source digest, or tag provenance does not match.

Never force-move a published tag, rewrite contributor history, or silently
replace release assets.
