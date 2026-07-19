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
