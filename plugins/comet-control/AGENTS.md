# comet-control — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.cursor-plugin/` or `.codex-plugin/` to this package.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## After the client has loaded this package

1. Requires Comet.app. Never load the extension in Google Chrome.
2. First load: unpack `plugin/comet_control/extension` in the logged-in Comet profile at `chrome://extensions` (use agent-computer-use for that chrome:// page if needed).
3. Verify probe: from this directory, `./scripts/ensure-broker.sh probe --json` reports `success: true`, `runtime_verified: true`, `extension_connected: true`.

Behavior after install: `skills/comet-control/SKILL.md`.
