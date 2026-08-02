# Third-party notices

ClarifyVoice includes or depends on third-party software. Each component remains
under its own license; the project MIT license does not replace those terms.

## Bundled in the Windows executable

| Component | Purpose | License information |
| --- | --- | --- |
| SoX 14.4.2 and its bundled codecs | Audio conversion | GPL-2.0-or-later; vendored text at `extra/sox-14.4.2/LICENSE.GPL.txt` |
| Python | Runtime | Python Software Foundation License |
| CustomTkinter | Desktop interface | MIT |
| Pillow | Image rendering | HPND |
| Requests | HTTP client | Apache-2.0 |
| sounddevice | Audio capture | MIT |
| PyInstaller bootloader | Portable packaging | GPL-2.0-or-later with a special exception for bundled applications |

PyInstaller is a build dependency. Its bootloader becomes part of the portable
executable under PyInstaller's documented exception.

The SoX runtime is invoked as a separate process and is not linked into the
ClarifyVoice source. Every tagged release provides `sox-14.4.2-source.tar.gz`
next to the Windows binary. The release workflow downloads the archive from the
[official SoX 14.4.2 files](https://sourceforge.net/projects/sox/files/sox/14.4.2/)
and requires this SHA-256 before publication:

```text
b45f598643ffbd8e363ff24d61166ccec4836fea6d3888881b8df53e3bb55f6c
```

The upstream Windows README names the codec and runtime projects used in that
distribution and is preserved at `extra/sox-14.4.2/README.win32.txt` and inside
the portable package.

## Optional local-ASR assets (not bundled)

The local-ASR groundwork can explicitly download the following assets through
the maintainer harness. They are not imported, downloaded, installed, or
included in the default ClarifyVoice runtime or release artifact.

| Component | Purpose | Version/model | License |
| --- | --- | --- | --- |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp/tree/v1.9.1) | CPU-only local transcription sidecar | v1.9.1 Windows x64 | MIT, copyright the ggml authors |
| [Whisper ggml small model](https://huggingface.co/ggerganov/whisper.cpp/blob/80da2d8bfee42b0e836fc3a9890373e5defc00a6/ggml-small.bin) | Multilingual local ASR weights converted for whisper.cpp | `ggml-small` | MIT per the upstream model card; derived from [OpenAI Whisper](https://github.com/openai/whisper) |

Exact URLs, sizes, SHA-256 digests, and extracted runtime file hashes are in
`local_asr_manifest.json`. These permissive licenses do not require a
corresponding-source offer. Their copyright and license notices must remain in
distributed ClarifyVoice documentation if the optional installer becomes a
user-facing feature. The complete notices are preserved in
`licenses/whisper.cpp-MIT.txt` and `licenses/openai-whisper-MIT.txt`; the
maintainer harness copies them beside an isolated installation. The upstream
warning not to run the example HTTP server with administrative privileges
applies; ClarifyVoice binds it only to loopback.

## Provider marks

The OpenAI, Gemini, and Groq marks under `assets/providers/` identify compatible
API providers. The names and marks remain the property of their respective
owners. Their presence does not imply endorsement or affiliation.

## Legacy prototype

The archived Electron prototype under `legacy/electron-prototype/` is not part
of the current build. If it is revived, its npm dependency licenses must be
reviewed and documented before distribution.

Maintainers should update this file whenever a distributed dependency or
third-party asset changes.
