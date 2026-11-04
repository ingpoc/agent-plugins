# Marketplace plugin activity hooks

These hooks report MCP calls to marketplace plugins as right-side marks on the ACU Dynamic Island. They do not add an app icon inside the pill or change its width. Only `acu` and `comet-control` control the existing inside ring. The hook skips `acu`, `agent-computer-use`, `comet-control`, `context-ledger`, and `developer-observability`, which already post their own activity.

Replace `/absolute/path/to/agent-computer-use` below with the installed `agent-computer-use` plugin directory. These are examples to add to the harness config; this plugin does not write user config. The hook accepts normalized plugin IDs matching `^[a-z0-9][a-z0-9-]{0,47}$`: it lowercases the MCP server name, changes spaces and underscores to hyphens, and removes one leading `plugin-` or `user-` prefix. Other names are ignored.

The hook reads JSON from stdin, retains only the harness, normalized server ID, and session ID (or process ID), and never logs or stores the input payload. Island posts honor `ACU_ISLAND_ACTIVITY=0`.

## Claude Code

Add the `PreToolUse` and `PostToolUse` entries to `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"/absolute/path/to/agent-computer-use/bin/island_hook.py\" claude-code start"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"/absolute/path/to/agent-computer-use/bin/island_hook.py\" claude-code finish"
          }
        ]
      }
    ]
  }
}
```

Claude Code names MCP tools `mcp__<server>__<tool>`; the hook extracts the server from `tool_name`.

## Cursor

Add these entries to `hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "beforeMCPExecution": [
      {
        "command": "python3 \"/absolute/path/to/agent-computer-use/bin/island_hook.py\" cursor start"
      }
    ],
    "afterMCPExecution": [
      {
        "command": "python3 \"/absolute/path/to/agent-computer-use/bin/island_hook.py\" cursor finish"
      }
    ]
  }
}
```

Cursor's before hook requires a permission result, so `cursor start` prints `{"permission":"allow"}` even when the payload is malformed or does not identify a plugin. The after hook prints nothing. The script reads `server`, `mcp_server`, or Cursor's `mcp_server_name` field, and falls back to a `mcp__<server>__<tool>` `tool_name` when present.

## Codex CLI

Current Codex CLI supports `PreToolUse` and `PostToolUse` command hooks. Add these entries to `config.toml`; the hooks feature must be enabled. The matcher covers MCP tool names (`mcp__<server>__<tool>`).

```toml
[features]
hooks = true

[hooks]

[[hooks.PreToolUse]]
matcher = '^mcp__'

[[hooks.PreToolUse.hooks]]
type = "command"
command = 'python3 "/absolute/path/to/agent-computer-use/bin/island_hook.py" codex start'

[[hooks.PostToolUse]]
matcher = '^mcp__'

[[hooks.PostToolUse.hooks]]
type = "command"
command = 'python3 "/absolute/path/to/agent-computer-use/bin/island_hook.py" codex finish'
```

The command hooks produce no stdout for Codex. See the [Codex CLI hooks configuration](https://github.com/openai/codex/blob/main/docs/config.md#hooks) for the current schema.

## Grok Build

No per-tool MCP hook API is documented in this repository, so Grok Build reporting is unsupported here. No hook snippet is provided.
