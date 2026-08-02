# Windows distribution and update security

This document defines the target installer, signing, update, recovery, and
incident contract. The repository contains no private key or reusable signing
credential. Until the prerequisites in [Rollout gates](#rollout-gates) are
complete, this is a fail-closed implementation contract rather than a claim
that published ClarifyVoice artifacts are already signed.

## Signing mechanism and ownership

ClarifyVoice uses [Azure Artifact Signing](https://azure.microsoft.com/en-us/products/artifact-signing)
(formerly Trusted Signing) with its public-trust certificate profile:

- The project owner, João Victor Maynard Mota, owns the Azure subscription,
  verified publisher identity, Artifact Signing account, and certificate
  profile.
- The release job authenticates through GitHub OIDC and a federated Microsoft
  Entra identity restricted to the protected `release-signing` environment.
  There is no client secret or exportable certificate private key in GitHub.
- The federated identity receives only the **Artifact Signing Certificate
  Profile Signer** role for the selected profile. The GitHub environment must
  require owner approval and tag release jobs must originate from protected
  `master` history.
- Azure protects the managed private key in FIPS 140-2 Level 3 HSMs. Release
  runners submit digests; the private key is not downloaded.
- The executable, MSI, and release-manifest CAB are signed with SHA-256 and an
  RFC 3161 timestamp from `http://timestamp.acs.microsoft.com`. Microsoft notes
  that Artifact Signing certificates are short lived, so timestamping is
  required for signatures to remain valid after certificate expiry.

As of 2026-08-02, Microsoft lists the Basic plan at USD 9.99/month for 5,000
signatures and USD 0.005 for each additional signature. The Premium plan is
USD 99.99/month for 100,000 signatures with the same overage. The owner must
check the [current official pricing](https://azure.microsoft.com/en-us/products/artifact-signing#pricing)
before provisioning because prices and regional availability can change.

Required protected GitHub configuration:

| Kind | Name | Purpose |
| --- | --- | --- |
| Secret | `AZURE_CLIENT_ID` | Federated application/client identity |
| Secret | `AZURE_TENANT_ID` | Microsoft Entra tenant |
| Secret | `AZURE_SUBSCRIPTION_ID` | Azure subscription |
| Variable | `AZURE_ARTIFACT_SIGNING_ENDPOINT` | Regional signing endpoint |
| Variable | `AZURE_ARTIFACT_SIGNING_ACCOUNT` | Artifact Signing account |
| Variable | `AZURE_ARTIFACT_SIGNING_PROFILE` | Public-trust certificate profile |

`distribution/update-policy.json` pins the legal publisher common name embedded
in the packaged app. After Azure identity validation, compare the actual signer
common name with this file. A mismatch must block release; never weaken the
check to accept an arbitrary trusted certificate.

## Installer behavior

`scripts/build-installer.ps1` uses pinned WiX Toolset 6.0.2 to build a per-user
MSI. If the project is used to generate revenue, the owner must review WiX's
[Open Source Maintenance Fee](https://github.com/wixtoolset/wix/releases/tag/v6.0.2)
terms.

| Operation | Defined behavior |
| --- | --- |
| Install | Installs the signed executable and notices under `%LOCALAPPDATA%\Programs\ClarifyVoice`; creates per-user Desktop and Start Menu shortcuts. |
| Upgrade | Windows Installer performs a major upgrade transaction. `%APPDATA%\ClarifyVoice` is outside the MSI and is never copied, migrated, or removed. |
| Repair | `msiexec /fa ClarifyVoice-windows-x64.msi` repairs program files and shortcuts without modifying user data. |
| Autostart | The installer does not enable autostart. The existing in-app setting owns the HKCU Run value. Upgrade preserves it; uninstall removes a stale ClarifyVoice Run value. |
| Failed upgrade | Windows Installer rolls the package transaction back. The previous installed product and all user data remain available. |
| Manual rollback | A user may run an older, still-trusted ClarifyVoice MSI. The MSI permits this explicit rollback, while the in-app update checker refuses every downgrade. |
| Uninstall | Removes installed program files, shortcuts, install metadata, and the autostart entry. Settings, usage statistics, and credentials remain in `%APPDATA%\ClarifyVoice` for recovery or reinstall. |
| Portable | The signed portable EXE and ZIP remain supported and do not participate in MSI registration. Manual checksum/signature verification remains documented. |

Do not run an installer while recording or processing text. The update UI
closes ClarifyVoice after starting the visible Windows Installer flow.

## Authenticated release manifest

The update checker is manual: **Settings → Check for updates**. It performs no
idle polling and no forced update.

1. Download `ClarifyVoice-release-manifest.cab` from the fixed
   `releases/latest/download` URL into a sibling `.part` file.
2. Require a valid Windows Authenticode chain and the publisher common name
   pinned inside the signed application.
3. Extract only `release-manifest.json` from the authenticated CAB.
4. Require the exact schema, stable channel, strict SemVer, matching `vX.Y.Z`
   tag, expected MSI name, canonical GitHub release URL, publisher, size, and
   lowercase SHA-256.
5. Refuse an older version and treat the installed version as no update.
6. Download the MSI atomically. Interruption, excess bytes, size mismatch, or
   checksum mismatch removes the `.part` file and leaves no runnable update.
7. Require a valid Authenticode signature from the same pinned publisher.
8. Offer the verified version to the user. Nothing executes without an
   explicit confirmation.
9. Recheck size, checksum, signature, and publisher immediately before calling
   visible `msiexec` UI.

The manifest CAB and MSI are cached only after verification. A failed launch
can be retried by checking again; user configuration is not part of the cache.
The existing GitHub release page, EXE, ZIP, and checksum path remain available
when the updater cannot reach or validate the manifest.

## CI and release gates

Pull-request CI builds an unsigned smoke-test executable and two MSI versions,
then exercises clean install, upgrade, repair, explicit rollback, uninstall,
shortcut cleanup, autostart cleanup, and `%APPDATA%` preservation on an
ephemeral Windows runner. It also creates and parses the release manifest and
CAB. These automated checks do not substitute for signed-artifact or real-user
manual acceptance.

`scripts/test-installer.ps1` is intentionally destructive and must never be run
on a developer workstation or a shared ClarifyVoice installation. It fails
closed unless `CI` and `GITHUB_ACTIONS` are true, the runner identifies itself
as a GitHub-hosted Windows runner, the repository matches `GITHUB_WORKSPACE`,
the profile roots have their expected hosted-runner layout, and every targeted
ClarifyVoice path and registry value is initially absent. Use only a disposable
VM for the separate manual lifecycle procedure. This script itself is restricted
to the hosted runner; do not bypass its guards.

The tag release workflow must fail unless it can:

1. match `version.py` to the tag;
2. sign and verify the EXE before embedding it in the MSI;
3. sign and verify the MSI before hashing it into the manifest;
4. sign and verify the manifest CAB;
5. match every signer to `distribution/update-policy.json` and require an RFC
   3161 timestamp;
6. generate separate SHA-256 files without modifying signed bytes;
7. create GitHub build-provenance attestations for EXE, MSI, CAB, and ZIP;
8. publish portable and installer assets from that same workflow run.

Never replace a published binary in place. Fixes require a new green commit,
annotated tag, signatures, manifest, attestations, and release.

## Rotation and emergency revocation

Planned rotation:

1. Create a new public-trust certificate profile under the same verified legal
   publisher.
2. Grant the federated release identity signer access to the new profile only.
3. Update the protected profile variable and run a non-publishing workflow
   dispatch.
4. Verify all three signatures, timestamp chains, publisher common name, and
   manifest identity.
5. Remove signer access from the old profile after a successful release. Keep
   old timestamped releases immutable.

Suspected compromise or wrongful signing:

1. Disable the GitHub federated credential and remove the profile signer role.
2. Revoke/disable the affected certificate profile in Azure and preserve Azure
   signing history, GitHub logs, workflow IDs, digests, and timestamps.
3. Disable the `release-signing` environment and private-report the incident.
4. Remove the compromised release or its manifest CAB so new update checks
   fail closed. Do not silently replace assets under the same tag.
5. Publish a security advisory identifying affected versions and instruct users
   to uninstall or roll back to a named trusted release as appropriate.
6. Rotate the profile/federation, patch on a new commit, and publish a new tag
   only after full CI, signature, provenance, and manual verification.

## Manual release acceptance

Before calling the distribution path production-ready, use a clean supported
Windows 10/11 account and record evidence for:

- publisher display and `Get-AuthenticodeSignature` on EXE, MSI, and CAB;
- clean install and both shortcuts;
- launch and provider configuration;
- upgrade with settings and credentials preserved;
- repair with settings and credentials preserved;
- interrupted download and failed upgrade recovery;
- explicit rollback to the previous signed MSI;
- uninstall cleanup with user data preserved;
- portable EXE/ZIP and checksum verification;
- SmartScreen behavior for the verified publisher.

## Rollout gates

Do not close issue #22 or enable/publish the update path until all of these are
true:

- secret storage issue #14 is merged and credential preservation is tested;
- provenance/locking issues #21 and #29 are merged and integrated;
- Azure identity validation, protected environment, OIDC federation, signer
  role, profile variables, and exact legal publisher pin are configured;
- a tagged release publishes trusted signatures and attestations;
- the manual acceptance matrix above is complete with evidence.
