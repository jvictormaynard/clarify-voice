## What changed

<!-- Describe the user-visible problem and your focused solution. -->

## Why

<!-- Link an issue with "Closes #123" when applicable. -->

## Validation

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m compileall -q app.py repositories.py secret_store.py update_security.py version.py desktop_state.py windows_hotkeys.py windows_clipboard.py provider_types.py provider_adapters.py provider_http.py provider_registry.py local_asr.py audio_file_batch.py scripts/create_release_manifest.py scripts/local_asr_harness.py tests`
- [ ] Built `ClarifyVoice.exe` when packaging or Windows integration changed
- [ ] Tested the real Windows UI when visuals, focus, hotkeys, audio, or tray behavior changed
- [ ] Added screenshots or a short recording for visual changes

## Safety and privacy

- [ ] No API keys, private transcripts, selected text, personal paths, or config files are included
- [ ] Documentation and tests were updated when behavior changed
- [ ] New third-party code or assets have compatible licenses and are documented

## Notes for reviewers

<!-- Mention tradeoffs, platform limitations, or follow-up work. -->
