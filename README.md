# ClarifyVoice Desktop Agent 

##  Setup Complete!

The application is now self-contained with **SoX included**. You do NOT need to install anything else.

##  How to Run

### Option 1: Run the Executable (Recommended)
Go to the `release/win-unpacked` folder and double-click:
**`ClarifyVoice.exe`**

### Option 2: Use the Start Script
Double-click **`start.bat`** in this folder.

##  Build Note
If you run `npm run build`, you might see an error at the end:
`ERROR: Cannot create symbolic link...`
**You can safely IGNORE this error.** The application is successfully built in the `release/win-unpacked` folder before this error occurs.

## Update the Windows installation

From this repository in WSL, run:

```bash
npm run deploy
```

The command builds a fresh Windows executable, finds the installation through
the `Clarify.lnk` Desktop shortcut, stops the running app, keeps the previous
executable as `ClarifyVoice.exe.backup`, installs the new build, and restarts it.

To deploy to a different location, set `CLARIFYVOICE_INSTALL_PATH` to the full
Windows executable path before running the command.

##  How to Use

1. **Launch the app**. You will see a small floating bar at the top-right.
   - Status: **Ready (Alt+L)**
2. **Press Alt + L** to start recording.
   - The bar will turn **RED** ("Recording...").
3. **Speak your message**.
4. **Press Alt + L** again to stop.
   - The bar will turn **BLUE** ("Processing...").
5. The text will be **automatically pasted** into your active window.

## Rewrite selected text

Select text in any Windows application and press **Alt + K**. ClarifyVoice sends
the selection to the configured **Text refinement** model, improves its clarity,
organization, spelling, grammar, and punctuation, then replaces the selection.
The original language and meaning are preserved.

ClarifyVoice pastes the rewrite only when the original window and selection are
still active. If either changed while the AI was processing, the result is left
in the clipboard and shown in the existing result panel instead. This feature
uses plain text, so rich-text formatting is not preserved.

Alt + K reuses the existing global keyboard hook and Windows clipboard APIs. It
does not add a background service, idle polling, or another runtime dependency.

## Transcription providers

Open **Settings** to select Gemini, OpenAI, or Groq and configure each provider's API
key, base URL, and model settings. ClarifyVoice stores these settings in
`%APPDATA%\ClarifyVoice\config.json` and automatically uses the selected
provider for future recordings.

- Gemini defaults to the official `generativelanguage.googleapis.com/v1beta`
  API and supports a custom root URL or a URL already ending in `/v1beta`.
- OpenAI uses an audio transcription model through `/v1/audio/transcriptions`.
- Groq defaults to `https://api.groq.com/openai/v1` and
  `whisper-large-v3-turbo`. It also supports `whisper-large-v3`.
- For ASR transcription, Prompt mode can refine the transcript with any LLM
  announced by any active provider. ASR, TTS, embedding, image, and realtime
  models are excluded from the refinement list. Gemini handles audio and text
  refinement in the same multimodal request, so it does not need a second model.
- Custom proxies must expose the corresponding provider-compatible endpoint.
  The currently deployed AMS `cliproxyapi` supports Gemini generation and
  OpenAI-compatible chat/responses, but does not currently expose
  `/v1/audio/transcriptions`; OpenAI Whisper therefore requires the official
  endpoint or another proxy that implements the Audio API.

##  Troubleshooting

- **"spawn sox ENOENT" Error**: This is fixed! The app now uses the bundled SoX binary.
- **Build Error**: As mentioned, ignore the "symbolic link" error during build.
