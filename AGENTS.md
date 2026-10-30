# Agent Plugins Repo Instructions

## Inheritance Contract

- Inherit global `AGENTS.md`; this file owns repo routing/facts, plugin `AGENTS.md` owns installation.
- Client setup owner: [Compatible clients](https://agent-plugins.org/compatible-clients).

## Durable facts

This repository is a **collection of portable [Agent Plugins](https://agent-plugins.org/specification)**.

| In the repo | Not in the repo |
| --- | --- |
| `plugins/<name>/` with closed `plugin.json`, `skills/`, optional `mcp.json` and `bin/`, plus required runtime files | Duplicate Cursor packages outside `plugins/<name>/`; Codex packages (`.codex-plugin/`) |
| Thin Codex, Cursor, Grok, and Claude catalogs pointing at portable packages; Claude lists only packages shipping `.claude-plugin/` | Duplicate client-specific plugin contents |

Install only `plugins/<name>/`; the collection root is not a plugin.

User supplies `https://github.com/ingpoc/agent-plugins` and plugin name; agent installs. Never ask the user to clone, symlink, edit settings, or follow recipes.

Forge: [Cursor Origin](https://cursor.com/codebase/gurusharan/agent-plugins); GitHub mirrors `main` outbound. Owner: [`docs/AGENTS.md`](docs/AGENTS.md).

## Trigger Map

| Trigger | First hop |
| --- | --- |
| Install a plugin | That plugin's `AGENTS.md`. Do not dump steps here. |
| Create, add, or update a portable plugin (layout, MCP packaging, Cursor package override, docs) | `workflow summary agent-plugin-routing-sync` → `workflow read agent-plugin-routing-sync --section "Create or Update"`. Fetch [spec](https://github.com/agentplugins/agent-plugins-spec), [plugin-authors](https://agent-plugins.org/plugin-authors), [manifest](https://agent-plugins.org/plugin-authors/manifest); if MCP also [mcp-servers](https://agent-plugins.org/plugin-authors/mcp-servers). Scaffold/validate: `python3 scripts/create_agent_plugin.py` then `python3 scripts/check.py`. Follow Repo Rules for required files and table updates. |
| Add or rename a plugin | `plugins/<name>/` → update the Plugins table here in the same pass |
| Change plugin behavior | That plugin's `SKILL.md` and `mcp.json` |
| Change how this plugin installs | That plugin's `AGENTS.md` |
| Change published benchmark scores | That plugin's `README.md` |
| Cursor MCP spawn / marketplace cache still relative or `${PLUGIN_*}` literal | That package's `.cursor-plugin/mcp.json`; context-ledger contract and bootstrap are in Repo Rules below. Dest repair: `scripts/install_cursor_dest.py`, only for pre-override relative installs. ACU `install_harness.py cursor-plugin` only retires the stale `~/.cursor/plugins/local` copy to the Trash |
| Change context-ledger MCP manifests or the serve launcher | `plugins/context-ledger/tests/test_manifests.py` and `tests/test_contract.py` (stdio shape, resolver, keychain, shell files reject or-true) |
| Checks, CI, pre-commit | `scripts/check.py` |
| Claim a plugin fix is done | Live-verify the installed client copy (MCP/CLI). Update the standing canvas from that run. `scripts/check.py` is not that proof. |
| Marketplace index stale after Origin main moves | `scripts/refresh_cursor_marketplace_index.sh`, run by the Origin mirror job. Team Auto Refresh does not cover the user-scoped registration. Recovery is remove, then add with no `--git-ref`. |

## Plugins

| Name | Path | Install |
| --- | --- | --- |
| `agent-computer-use` | `plugins/agent-computer-use/` | `plugins/agent-computer-use/AGENTS.md` |
| `comet-control` | `plugins/comet-control/` | `plugins/comet-control/AGENTS.md` |
| `context-ledger` | `plugins/context-ledger/` | `plugins/context-ledger/AGENTS.md` |

## Agent install (every client)

1. Detect the running client.
2. Open [compatible-clients](https://agent-plugins.org/compatible-clients). Match the client. Take its setup-instructions link. If the page has no cards (JS shell), read the owner [`lib/compatible-clients.ts`](https://raw.githubusercontent.com/agentplugins/agent-plugins-site/main/lib/compatible-clients.ts) and use that row's `instructionsUrl`.
3. Prefer the repository catalog for Codex (`.agents/plugins/marketplace.json`), Cursor (`.cursor-plugin/marketplace.json`), Claude Code (`.claude-plugin/marketplace.json`), or Grok Build (`.grok-plugin/marketplace.json`). Catalog package boundaries are defined above.
4. Read and apply `plugins/<name>/AGENTS.md`.
5. Verify client listing; repair via its setup page. Cursor: catalog entry missing from Customize → update or remove/re-add marketplace to re-index; never install a parallel local copy.
6. Stop only for uncompletable OS/store consent, or a missing client/setup link; report the blocker.


## Repo Rules

- One portable package per `plugins/<name>/`; the live spec owns shape.
- Additions require a Plugins row, `plugins/<name>/AGENTS.md`, and `plugins/<name>/README.md`; add/rename updates the table in the same pass.
- context-ledger ships portable `mcp.json`, `.cursor-plugin/mcp.json`, and `.claude-plugin/mcp.json`. Portable `mcp.json` is bare `sh` plus a resolver with no `${`. Cursor uses `sh`, one root arg `${CURSOR_PLUGIN_ROOT}`, no `cwd`, and the same cache/tag fallback. Portable args are the resolver, then `${PLUGIN_ROOT}`, `${CURSOR_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, then `serve` (Codex expands only `${PLUGIN_ROOT}`; a Cursor surface that reads the root manifest expands `${CURSOR_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_ROOT}`). Portable and `.cursor-plugin/mcp.json` set `CONTEXT_LEDGER_MCP_CLIENT_TOKEN` to `${env:CONTEXT_LEDGER_MCP_TOKEN}`. Claude Code uses `.claude-plugin/plugin.json` + `.claude-plugin/mcp.json` (`${CLAUDE_PLUGIN_ROOT}`) with no env interpolation. A legacy stdio `./bin/…-mcp` still ships in `.cursor-plugin/` and requires the Cursor override. Dest rewrite only repairs pre-override relative installs. Contract tests: `plugins/context-ledger/tests/test_contract.py` and `tests/test_manifests.py`.
- Cloud: plugin not materialized. Cursor cloud can spawn `.cursor-plugin/mcp.json` with no plugin directory on the machine, so `${CURSOR_PLUGIN_ROOT}` stays literal. That manifest is `sh` with no `cwd`. The resolver execs a root only when its `plugin.json` version is at least the pinned version, else the highest `context-ledger` launcher under `~/.cursor/plugins/cache` that meets that version, else downloads tag `context-ledger-v<version>` (the `plugin.json` version, one literal per manifest) into the user cache. An older materialized copy is not executed. The portable resolver keeps its root checks, then those two fallbacks. Each release must push tag `context-ledger-v<version>` after merge. Failure is stderr `PLUGIN_BOOTSTRAP_FAILED`.
- Before shipping changed installed runtime/skills/references/install docs, require a `plugin.json` SemVer bump and aligned runtime versions/assertions. Tests-only and collection tooling are exempt.
- This repo is public. No personal case data.
