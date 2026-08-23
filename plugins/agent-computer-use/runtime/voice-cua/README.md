# Voice CUA runtime

Packaged Samantha runtime owned by `agent-computer-use`.

- `python/voice_cua/`: gateway, GPT Realtime session, audio, tools, island bus, settings, and Keychain metadata access.
- `config/.secret/`: metadata-only templates; secret values remain in macOS Keychain.
- `scripts/build_voice_helper.py`: builds the nested `voice-cua.app` from this package and the adjacent `skills/macos-cua` runtime.
- `tests/test_voice_cua.py`: source-level runtime checks.

Build and installation are owned by `skills/macos-cua/service/install_service.py`.
