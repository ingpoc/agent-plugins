# Agent Plugins Repo Instructions

## Inheritance Contract

- Inherit universal doctrine from the global `AGENTS.md`. This file owns routing, triggers, and durable repo facts. It does not own per-plugin install.
- Client setup pages are not owned here. [Compatible clients](https://agent-plugins.org/compatible-clients) is.

## Durable facts

This repository is a **collection of portable [Agent Plugins](https://agent-plugins.org/specification)**.

| In the repo | Not in the repo |
| --- | --- |
| `plugins/<name>/` with closed `plugin.json`. context-ledger ships three MCP manifests: portable `mcp.json` (bare `sh` resolver, no `${` in the script; root args `${PLUGIN_ROOT}`, `${CURSOR_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, then cache and pinned-tag bootstrap), `.cursor-plugin/` (`sh`, one root arg `${CURSOR_PLUGIN_ROOT}`, no `cwd`, same fallback), `.claude-plugin/` (`${CLAUDE_PLUGIN_ROOT}`). Client-token env is on the portable and Cursor manifests only. A legacy stdio `./` command still ships `.cursor-plugin/` | Duplicate Cursor packages outside `plugins/<name>/` |
| `skills/`, optional `mcp.json`, optional `bin/`, runtime files that package needs | Codex plugin packages (`.codex-plugin/`) |
| Thin Codex, Cursor, Grok, and Claude marketplace catalogs that point at the same portable packages. `.claude-plugin/marketplace.json` lists only plugins that ship `.claude-plugin/` | Duplicate client-specific copies of plugin contents |

The collection root is not a plugin. The install unit is always `plugins/<name>/`.

The user gives their agent the GitHub URL (`https://github.com/ingpoc/agent-plugins`) and a plugin name. The agent does the install. The user does not clone, symlink, edit settings, or follow a recipe.

Cursor Origin (`https://cursor.com/codebase/gurusharan/agent-plugins`) is the forge of record. GitHub is the outbound mirror of `main`. See [`docs/AGENTS.md`](docs/AGENTS.md).

## Trigger Map

| Trigger | First hop |
| --- | --- |
| Install a plugin | That plugin's `AGENTS.md`. Do not dump steps here. |
| Create, add, or update a portable plugin (layout, MCP packaging, Cursor package override, docs) | `workflow summary agent-plugin-routing-sync` → `workflow read agent-plugin-routing-sync --section "Create or Update"`. Fetch [spec](https://github.com/agentplugins/agent-plugins-spec), [plugin-authors](https://agent-plugins.org/plugin-authors), [manifest](https://agent-plugins.org/plugin-authors/manifest); if MCP also [mcp-servers](https://agent-plugins.org/plugin-authors/mcp-servers). Scaffold/validate: `python3 scripts/create_agent_plugin.py` then `python3 scripts/check.py`. Land in `plugins/<name>/` with plugin `AGENTS.md` + `README.md`. Add/update the Plugins table here in the same pass. |
| Add or rename a plugin | `plugins/<name>/` → update the Plugins table here in the same pass |
| Change plugin behavior | That plugin's `SKILL.md` and `mcp.json` |
| Change how this plugin installs | That plugin's `AGENTS.md` |
| Change published benchmark scores | That plugin's `README.md` |
| Cursor MCP spawn / marketplace cache still relative or `${PLUGIN_*}` literal | That package's `.cursor-plugin/mcp.json`. context-ledger uses `sh` (no `cwd`): a real `${CURSOR_PLUGIN_ROOT}`, else a cache copy, else tag `context-ledger-v<version>`. Portable `mcp.json` keeps root args `${PLUGIN_ROOT}`, `${CURSOR_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, then the same fallback. Dest rewrite (`install_harness.py cursor-plugin` or `scripts/install_cursor_dest.py`) only repairs pre-override relative installs |
| Change context-ledger MCP manifests or the serve launcher | `plugins/context-ledger/tests/test_manifests.py` and `tests/test_contract.py` (stdio shape, resolver, keychain, shell files reject or-true) |
| Checks, CI, pre-commit | `scripts/check.py` |
| Claim a plugin fix is done | Live-verify the installed client copy (MCP/CLI). Update the standing canvas from that run. `scripts/check.py` is not that proof. |

## Plugins

| Name | Path | Install |
| --- | --- | --- |
| `agent-computer-use` | `plugins/agent-computer-use/` | `plugins/agent-computer-use/AGENTS.md` |
| `comet-control` | `plugins/comet-control/` | `plugins/comet-control/AGENTS.md` |
| `context-ledger` | `plugins/context-ledger/` | `plugins/context-ledger/AGENTS.md` |

## Agent install (every client)

1. Detect the running client.
2. Open [compatible-clients](https://agent-plugins.org/compatible-clients). Match the client. Take its setup-instructions link. If the page has no cards (JS shell), read the owner [`lib/compatible-clients.ts`](https://raw.githubusercontent.com/agentplugins/agent-plugins-site/main/lib/compatible-clients.ts) and use that row's `instructionsUrl`.
3. Prefer the repository catalog for Codex (`.agents/plugins/marketplace.json`), Cursor (`.cursor-plugin/marketplace.json`), Claude Code (`.claude-plugin/marketplace.json`), or Grok Build (`.grok-plugin/marketplace.json`). All catalogs load **`plugins/<name>/` only** (the directory with `plugin.json`); the collection root is never a plugin. The Claude catalog lists only plugins that ship `.claude-plugin/`.
4. Read and apply `plugins/<name>/AGENTS.md`.
5. Verify the client lists the plugin. Fix from the same setup page if it does not. On Cursor: if the plugin is in `.cursor-plugin/marketplace.json` but missing under this marketplace in Customize, remove and re-add (or update) the marketplace so the catalog re-indexes — do not install a parallel local copy as the fix.
6. Stop only for an OS or store consent the agent cannot complete.

If the client is missing or has no setup link, say so and stop.

## Repo Rules

- One portable package per `plugins/<name>/`. Shape comes from the live spec, not this file.
- Adding a plugin without a Plugins row, `plugins/<name>/AGENTS.md`, and `plugins/<name>/README.md` is incomplete.
- context-ledger portable `mcp.json` is bare `sh` plus a resolver with no `${`. Args are the resolver, then `${PLUGIN_ROOT}`, `${CURSOR_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, then `serve` (Codex expands only `${PLUGIN_ROOT}`; a Cursor surface that reads the root manifest expands `${CURSOR_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_ROOT}`). Portable and `.cursor-plugin/mcp.json` set `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` to `${env:CONTEXT_LEDGER_MCP_TOKEN}`. Claude Code uses `.claude-plugin/plugin.json` + `.claude-plugin/mcp.json` (`${CLAUDE_PLUGIN_ROOT}`) with no env interpolation. A legacy `./bin/…-mcp` still requires the Cursor override. Dest rewrite only repairs pre-override relative installs. Contract tests: `plugins/context-ledger/tests/test_contract.py` and `tests/test_manifests.py`.
- Cloud: plugin not materialized. Cursor cloud can spawn `.cursor-plugin/mcp.json` with no plugin directory on the machine, so `${CURSOR_PLUGIN_ROOT}` stays literal. That manifest is `sh` with no `cwd`. The resolver execs a root only when its `plugin.json` version is at least the pinned version, else the highest `context-ledger` launcher under `~/.cursor/plugins/cache` that meets that version, else downloads tag `context-ledger-v<version>` (the `plugin.json` version, one literal per manifest) into the user cache. An older materialized copy is not executed. The portable resolver keeps its root checks, then those two fallbacks. Each release must push tag `context-ledger-v<version>` after merge. Failure is stderr `PLUGIN_BOOTSTRAP_FAILED`.
- Before shipping changed installed package contents (runtime, skill, references, or install docs), bump that plugin's `plugin.json` SemVer; keep any in-package runtime version and assertions aligned. Skip test-only and collection-tooling changes.
- This repo is public. No personal case data.
