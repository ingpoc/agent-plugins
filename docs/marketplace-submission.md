# Marketplace submission prep

Status: **prep only**. Nothing has been submitted to cursor.com/marketplace/publish or to `xai-org/plugin-marketplace`.

Plugins: `agent-computer-use`, `comet-control`, `context-ledger`. (`irctc-train-booking` stays in this repo and is not part of this submission.)

Sources checked: Cursor [Plugins reference → Submitting a plugin](https://cursor.com/docs/reference/plugins), xAI [CONTRIBUTING.md](https://github.com/xai-org/plugin-marketplace/blob/main/CONTRIBUTING.md) (including Security expectations), and the `review-plugin-submission` skill (create-plugin).

## Descriptions

| Plugin | Description (same text in `plugin.json`, `.cursor-plugin/`, `.grok-plugin/`, `.claude-plugin/` and every catalog) |
| --- | --- |
| `agent-computer-use` | Operate native macOS apps from an agent through two Accessibility-first MCP tools (state and act), with an optional GPT Realtime voice runtime. macOS only. |
| `comet-control` | Drive and verify web apps in one leased, visible Perplexity Comet window per agent, through a local broker and an unpacked Comet extension. Never touches Google Chrome or its profiles. macOS only. |
| `context-ledger` | Local-first decision memory for agents: save the decisions and lessons from critical or recurring tasks, then look them up later through a scoped MCP server over a local SQLite ledger. |

## Reviewer testing instructions

Each one takes a few minutes on a Mac. All three plugins are **macOS only**.

### agent-computer-use

Needs macOS 13 or later (Apple silicon tested) and `python3` on `PATH`.

1. Download the notarized **Agent Computer Use** DMG from this repository's GitHub release, open it, and drag **Agent Computer Use** to Applications. The app is Developer ID signed and notarized, so Gatekeeper opens it without warnings. Building from source needs your own Apple code-signing identity (`install_service.py --team <TEAM_ID>`), so reviewers should use the DMG.
2. Open **Agent Computer Use**. When asked, grant **Accessibility** and **Screen Recording** (System Settings → Privacy & Security), then quit and reopen the app.
3. Install the `agent-computer-use` plugin and restart the client. The MCP server `agent-computer-use` should list exactly two tools: `state` and `act`.
4. Prompt: *"Use agent-computer-use to open Calculator and compute 12 × 3."* Expected: the agent calls `state` and then `act`, Calculator shows **36**, and the `act` reply includes a verified change.
5. Optional, needs your own OpenAI key: the Samantha voice runtime. Skip it for a functional review. Steps 1 to 4 do not need it.
6. Cleanup: quit Agent Computer Use, move it from Applications to the Trash, and remove its Accessibility and Screen Recording entries.

Network: steps 1 to 4 make no network calls. Voice, when enabled, calls `api.openai.com` with a key the user stores in the macOS Keychain.

### comet-control

Needs macOS, [Perplexity Comet](https://www.perplexity.ai/comet) installed and opened once, and `python3`.

1. Install the `comet-control` plugin. In a terminal, `cd` into the installed plugin directory (the folder that contains `plugin.json`).
2. Load the extension once. In Comet, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and select `plugin/comet_control/extension`.
3. Start and probe the local broker:
   ```sh
   ./scripts/ensure-broker.sh start
   ./scripts/ensure-broker.sh probe --json
   ```
   Expected: `"success": true`, `"runtime_verified": true`, `"extension_connected": true`.
4. Lease a window, read it, and close it:
   ```sh
   W="$HOME/comet-review"
   python3 skills/comet-control/scripts/durable_lease_controller.py start --session-id review1 --label review --url https://example.com --workdir "$W"
   python3 skills/comet-control/scripts/durable_lease_controller.py send --workdir "$W" '{"actions":[{"type":"page_context"}]}'
   python3 skills/comet-control/scripts/durable_lease_controller.py closeout --workdir "$W"
   ```
   Expected: a new visible Comet window opens at example.com, `page_context` reports the title "Example Domain", and closeout reports `"verified_absent": true` and closes only that window. Other Comet tabs are untouched.
5. Or prompt the agent: *"Use comet-control to open https://example.com and tell me the page heading."*
6. Cleanup: remove the extension in `chrome://extensions` and delete `$HOME/comet-review`.

Network: the broker listens only on a Unix socket and `ws://127.0.0.1:38927`. The extension's broad permissions (`debugger`, `<all_urls>`, `history`, `downloads`, clipboard) let it drive whichever site the agent leases. It acts only in the leased window.

### context-ledger

Needs macOS (arm64 tested) and Python 3.11 or later.

1. Install the `context-ledger` plugin. In a terminal, `cd` into the installed plugin directory.
2. Create a throwaway ledger:
   ```sh
   export CONTEXT_LEDGER_DATA="$HOME/context-ledger-review"
   python3 bin/context_ledger.py --data "$CONTEXT_LEDGER_DATA" setup
   python3 bin/context_ledger.py --data "$CONTEXT_LEDGER_DATA" init --scope review --actor reviewer
   python3 bin/context_ledger.py --data "$CONTEXT_LEDGER_DATA" doctor
   ```
   Expected: each prints `"ok": true`. `doctor` reports `binding`, `schema` and `sqlite_ok` as true. `setup` downloads hash-pinned packages from PyPI once.
3. MCP smoke test without a client:
   ```sh
   { printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"review","version":"0"}}}' '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 3; } | bin/context-ledger-mcp --data "$CONTEXT_LEDGER_DATA" serve
   ```
   Expected: the second reply lists `append_event`, `browse`, `find`, `get`, `record`, `save` (plus read-only `ledger_activity`, `ledger_query`, `ledger_stats`).
4. In the client (start it from a shell where `CONTEXT_LEDGER_DATA` is set, or run step 2 without `--data` to use `~/.context-ledger`), prompt: *"Use context-ledger to save this agent conclusion for scope review: run the unit tests before opening a PR, because CI takes 20 minutes. Then find decisions about opening a PR."* Expected: `save` returns a decision id, and `find` returns that record.
5. Cleanup: move `$HOME/context-ledger-review` to the Trash.

Network: local mode makes no calls after `setup`. The launcher downloads the tagged plugin source from `codeload.github.com` only when no installed copy of the current version exists. The optional remote bridge sends a user-provided bearer token to a user-provided `CONTEXT_LEDGER_MCP_URL`.

## Checklist results (branch `acu/marketplace-prep`)

P = pass, F = fail or blocker, N = note.

| Check | agent-computer-use | comet-control | context-ledger |
| --- | --- | --- | --- |
| Valid manifest (root `plugin.json` Agent Plugin, closed schema) | P | P | P |
| `.cursor-plugin/plugin.json` | P (name, version, description, mcpServers only, as `scripts/check.py` requires) | N (none; Cursor loads the root Agent Plugin, no MCP) | P |
| `.grok-plugin/plugin.json` (new) | P | P | P |
| Unique kebab-case name | P | P | P |
| Clear description | P (rewritten) | P (rewritten) | P (rewritten) |
| License stated (MIT, root `LICENSE`, every manifest and catalog entry) | P | P | P |
| README: purpose, install, configuration, testing, endpoints and credentials | P (new Overview) | P (new Overview) | P (new Overview) |
| Paths relative, no `..` or absolute paths | P | P | P |
| Skills frontmatter (`name`, `description`) | P | P | P |
| Marketplace entries (`.cursor-plugin`, `.grok-plugin`, `.agents`; `.claude-plugin` for ledger) | P | P | P |
| 1:1 logo with background plate, committed, relative path | P (Island Cursor, `assets/logo.svg` 1024², plus `logo.png`) | P (Island Cursor family, `assets/logo.svg` 1024²) | P (Island Cursor family, `assets/logo.svg` 1024²) |
| Homepage | P | P | P |
| Brand-scoped keywords | P | P | P |
| Security: no curl-to-shell or remote eval | P | P | **N:** the MCP launcher fallback downloads the tagged source tarball and executes it (pinned to tag `context-ledger-v<version>`, verifies the version only, no hash) |
| Security: hooks or MCP scope | P (two scoped tools, no hooks) | N (no MCP; extension has broad permissions, documented) | P (no hooks) |
| Security: obfuscation | P | P (extension JS is readable source) | N (`ui/ledger-v2.html` embeds base64 fonts; generated from `ui-src/` and checked by `build.py --check`) |
| Network endpoints and credentials declared in README | P | P | P |
| Tested locally (`scripts/check.py`) | P | P | P |
| Source legitimacy (public repo, official org) | P (`ingpoc/agent-plugins` is public) | P | P |
| Pinned SHA reachable | open until the branch merges and a SHA is chosen | open | open |

## Still needed before submitting

1. **context-ledger logo**: done. `plugins/context-ledger/assets/logo.svg` (Island Cursor family, 1:1 with a background plate) is set as `logo` in its `.grok-plugin/plugin.json` and `.cursor-plugin/marketplace.json` entry. comet-control's logo was replaced with the same family.
2. **agent-computer-use notarized release**: `acu/samantha-reliability` (0.2.26, notarized build and onboarding) is merged into `main` (PR #20) and into this branch. Still to do: publish the notarized DMG as a GitHub release asset and put its URL in step 1 above.
3. **context-ledger remote default**: `bin/remote_bridge.py` still defaults `CONTEXT_LEDGER_MCP_URL` to the owner's Tailscale Funnel host. The docs are generalized. The code was left alone because cloud agents rely on that default. To remove it, set `CONTEXT_LEDGER_MCP_URL` as a runtime secret, make the default empty, and fail with a named error when it is unset.
4. **context-ledger bootstrap**: for review, either pin the download to a commit SHA and verify a sha256, or drop the download fallback in the marketplace build.
5. **Versions**: descriptions and manifests changed without a version bump. Bump through each plugin's release flow (`plugins/context-ledger/scripts/bump-version.sh` also pushes a tag).
6. Pick the public commit to ship, then run `git ls-remote https://github.com/ingpoc/agent-plugins.git <ref>`.

## Draft xAI catalog entries (do not submit yet)

Add to `xai-org/plugin-marketplace` `.grok-plugin/marketplace.json`, then run `python3 scripts/generate-plugin-index.py` and `python3 scripts/validate-catalog.py`.

```json
{
  "name": "agent-computer-use",
  "description": "Operate native macOS apps from an agent through two Accessibility-first MCP tools (state and act), with an optional GPT Realtime voice runtime. macOS only.",
  "category": "productivity",
  "source": {"source": "url", "url": "https://github.com/ingpoc/agent-plugins.git", "sha": "<40-char sha>", "path": "plugins/agent-computer-use"},
  "homepage": "https://github.com/ingpoc/agent-plugins/tree/main/plugins/agent-computer-use",
  "keywords": ["agent-computer-use", "agent computer use", "cuaservice", "macos-cua"]
}
```

```json
{
  "name": "comet-control",
  "description": "Drive and verify web apps in one leased, visible Perplexity Comet window per agent, through a local broker and an unpacked Comet extension. Never touches Google Chrome or its profiles. macOS only.",
  "category": "productivity",
  "source": {"source": "url", "url": "https://github.com/ingpoc/agent-plugins.git", "sha": "<40-char sha>", "path": "plugins/comet-control"},
  "homepage": "https://github.com/ingpoc/agent-plugins/tree/main/plugins/comet-control",
  "keywords": ["comet-control", "comet control", "perplexity comet", "comet browser"]
}
```

```json
{
  "name": "context-ledger",
  "description": "Local-first decision memory for agents: save the decisions and lessons from critical or recurring tasks, then look them up later through a scoped MCP server over a local SQLite ledger.",
  "category": "productivity",
  "source": {"source": "url", "url": "https://github.com/ingpoc/agent-plugins.git", "sha": "<40-char sha>", "path": "plugins/context-ledger"},
  "homepage": "https://github.com/ingpoc/agent-plugins/tree/main/plugins/context-ledger",
  "keywords": ["context-ledger", "context ledger"]
}
```

Cursor: submit `https://github.com/ingpoc/agent-plugins` at cursor.com/marketplace/publish. The repo-root `.cursor-plugin/marketplace.json` lists the plugins. The draft entries omit `domains` because none of these plugins owns a web domain.
