# agent-computer-use — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root.

## After the client has loaded this package

1. If Cursor: `python3 skills/macos-cua/scripts/install_harness.py cursor-plugin` from this directory. Dest `mcp.json` `command` must be the absolute dest launcher.
2. `python3 skills/macos-cua/service/install_service.py` builds the packaged `runtime/voice-cua/` helper, nests and signs it inside CUAService, then installs the app. No sibling Voice CUA checkout is required.
3. Grant **Accessibility** and **Screen Recording** to **macos-cua Service** / **CUAService**. Relaunch the service after Screen Recording.
4. Client lists `agent-computer-use` tools **`state`** and **`act`** only.

Published scores: this plugin's `README.md`. Refresh only from a warm `python3 skills/macos-cua/scripts/run_benchmarks.py --repeat 5 --rate`.

Behavior: `skills/macos-cua/SKILL.md`. WhatsApp send/attach: `$whatsapp`, not this package.
