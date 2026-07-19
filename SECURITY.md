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
- Provider API keys are stored as plain text in the current user's
  `%APPDATA%\ClarifyVoice\config.json`. Protect the Windows account and do not
  share this file.
- Portable executables are not currently code-signed. Verify the SHA-256 file
  attached to a release.
- Custom endpoints can receive the same content as an official provider. Only
  configure endpoints you trust.
- The app intentionally does not bundle `.env`; a build that contains one must
  not be distributed.

Accidental key exposure should be handled by revoking the key at the provider,
creating a replacement, removing it from local files or logs, and checking the
Git history before publication.
