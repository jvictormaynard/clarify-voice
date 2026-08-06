## What changed

<!-- Describe the user-visible problem and your focused solution. -->

## Why

<!-- Link an issue with "Closes #123" when applicable. -->

## Validation

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m compileall -q spikes/pyside6/qml_app.py spikes/pyside6/qml_bridge.py spikes/pyside6/qml_runtime.py spikes/pyside6/qml_settings.py spikes/pyside6/qml_audio_batch.py spikes/pyside6/qml_clipboard.py spikes/pyside6/qml_voice_translation.py spikes/pyside6/qt_shell.py repositories.py workflow_config.py workflow_settings.py voice_translation.py dictionary_snippets.py microphone_controls.py secret_store.py update_security.py version.py desktop_state.py windows_hotkeys.py windows_clipboard.py provider_types.py provider_adapters.py provider_http.py provider_registry.py local_asr.py audio_file_batch.py audio_file_batch_ui.py history_store.py scripts/create_release_manifest.py scripts/local_asr_harness.py tests`
- [ ] Confirmed `spikes/pyside6/qml/Main.qml` and the complete QML asset directory are present
- [ ] Built `ClarifyVoice.exe` when packaging or Windows integration changed
- [ ] Tested the real Windows UI when visuals, focus, hotkeys, audio, or tray behavior changed
- [ ] Added screenshots or a short recording for visual changes

## Safety and privacy

- [ ] No API keys, private transcripts, selected text, personal paths, or config files are included
- [ ] Documentation and tests were updated when behavior changed
- [ ] New third-party code or assets have compatible licenses and are documented

## Notes for reviewers

<!-- Mention tradeoffs, platform limitations, or follow-up work. -->
