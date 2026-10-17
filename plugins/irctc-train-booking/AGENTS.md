# irctc-train-booking — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.codex-plugin/` to this package. A stdio `./` command also ships `.cursor-plugin/plugin.json` and `.cursor-plugin/mcp.json` (`${CURSOR_PLUGIN_ROOT}`).

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

1. Requires the installed comet-control plugin. Runs use that plugin's lease controller, `skills/comet-control/scripts/durable_lease_controller.py`. Do not modify comet-control from this package.
2. Requires macOS iPhone Mirroring. Build `skills/irctc-train-booking/scripts/ocr.swift` with `swiftc -O ocr.swift -o ocr` and keep `ocr` beside those scripts. OCR reads the mirrored Messages screen for the Aadhaar OTP.
3. `login.py` and `preflight.sh` read the macOS Keychain service `irctc` (accounts `username` and `password`) at runtime. Do not commit those values or pass them on argv.
4. Copy `skills/irctc-train-booking/scripts/` into `~/irctc/` before a run. Behavior: `skills/irctc-train-booking/SKILL.md`.
