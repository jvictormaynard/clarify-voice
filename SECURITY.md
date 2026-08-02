# Security policy

## Supported versions

Security fixes target the newest published release and the current default
branch. Older portable executables may not receive patches.

## Report a vulnerability

Please do not open a public issue for a vulnerability or suspected credential
exposure.

Use GitHub's private vulnerability reporting at:

<https://github.com/jvictormaynard/clarify-voice/security/advisories/new>

If private reporting is unavailable, contact the maintainer through the links
on [their GitHub profile](https://github.com/jvictormaynard) and share only the
minimum information needed to establish a private channel.

Include:

- affected version or commit;
- operating system and installation method;
- impact and realistic attack scenario;
- reproduction steps or proof of concept;
- suggested mitigation, if known.

Do not include a live API key, private transcript, or another person's data.
You should receive an acknowledgement within seven days. Disclosure timing will
be coordinated after impact and remediation are understood.

## Security model and known limitations

- ClarifyVoice sends audio or selected text directly to the configured AI
  provider or custom endpoint. That provider's retention and privacy terms
  apply.
- Provider API keys are encrypted with current-user Windows DPAPI and stored in
  `%APPDATA%\ClarifyVoice\secrets.dpapi.json`; ordinary settings remain in
  `config.json`. Copying the encrypted file to another Windows user or machine
  does not make it decryptable.
- Linux and macOS source runs are experimental and use a separately documented
  plaintext `~/.clarifyvoice/secrets.json` fallback with owner-only permissions
  where the filesystem supports them. Do not share that file.
- Portable executables are not currently code-signed. Verify the SHA-256 file
  attached to a release.
- The managed signing and secure update rollout is tracked in
  [Windows distribution security](docs/windows-distribution.md) and must remain
  fail-closed until its prerequisites and manual acceptance gates are complete.
- Custom endpoints can receive the same content as an official provider. Only
  configure endpoints you trust.
- The app intentionally does not bundle `.env`; a build that contains one must
  not be distributed.
- The local-ASR groundwork is not integrated into the product yet. Its optional
  assets must match the committed SHA-256 manifest before extraction or
  execution. The sidecar binds only to loopback behind a random request path,
  refuses to launch when Windows reports elevation or cannot verify privilege
  state, and is not a sandbox against another malicious process running as the
  same Windows user.

Accidental key exposure should be handled by revoking the key at the provider,
creating a replacement, removing it from local files or logs, and checking the
Git history before publication.

Deleting only `ClarifyVoice.exe` leaves local data in place. Remove the secret
file or the whole ClarifyVoice data directory to erase stored credentials.

For a suspected release-signing compromise, immediately disable the federated
release identity and signer role, revoke the affected Artifact Signing profile,
preserve audit evidence, remove the compromised manifest/release without
replacing assets in place, publish an advisory, and recover on a new commit and
tag. The complete procedure is in
[Windows distribution security](docs/windows-distribution.md#rotation-and-emergency-revocation).
