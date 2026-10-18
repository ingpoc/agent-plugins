# Ledger scope v3

Applicability scope is one token on the record: `fleet`, `project:<id>`, or `tool:<id>`. Fleet is the widest scope. A record with no scope token is not treated as fleet and stays out of `find`.

## Scope mapping

`project:<id>` is used only when `<id>` is a real project the caller can bind.

Built-in project ids:

- `agent-plugins`
- `open-resource-network`
- `aadhaarchain`
- `brandgpt`
- `hisab-pune`
- `databricks-interview`

`data/project-registry.json` key `projects`, and `binding.json` keys `project` and `projects`, add more ids. `binding.json` `scope_id` is the ledger partition. It is not added to the project allowlist.

`tool:<id>` is used only for a known tool id: `comet`, `cursor-cli`, `codex-cli`, `git`, `python`, `render`, `kite`.

Every other entity is `fleet`. That includes free-form topics such as skills, keychain, chrome, shell, and travel. A topic name is not a project.

An entity named `fleet` is `fleet`, never `project:fleet`. An explicit fleet cue in the decision, why, or applicability is also `fleet`: any, every, or another project; not about this project; the word fleet; every bot or agent; all bots, agents, or agent projects; cross-project; a standing rule; or never ... on the Mini.

The entity `context-ledger` names part of `agent-plugins` and counts as that project.

Otherwise exactly one allowlisted project entity sets `project:<id>`, also when a known tool is named. No code or product wording is needed. Two or more allowlisted projects set `fleet`.

With no allowlisted project, one known tool entity sets `tool:<id>`. More than one known tool, or none, sets `fleet`. When unsure, pick the wider scope.

A record that already has a legal scope token keeps it. `project:fleet` and a project or tool id outside the allowlists are inferred again.

## Volatile detector

Volatile tokens are:

- version strings, including an optional `v` prefix (`1.2.3`, `v1.2`)
- commit SHAs of 7 to 40 hex characters that contain at least one `a-f`
- PR or Project ids (`pr 19`, `pull request 15`, `project 3`, `pull/15`)
- dates (`2026-10-17`, `10/17/2026`, `Oct 17, 2026`)

A model name is masked before the version scan, so `gpt-6.1` is not a version pin. A status word anywhere in the text is not volatile on its own.

A save is a pure status save only when the decision's main claim is a state. Examples: "X is pending", "the pull request is merged", "status update", or a short line that ends in pending, open, merged, blocked, or failed. Rule cues (`must`, `never`, `always`, `only`, `because`, `rule`, `when`) mean the text is not pure status. "CI must stay passing before merge" stays a rule.

`save` refuses a pure status save and refuses volatile tokens in the decision text. Those tokens belong in evidence. `kind: state` is refused.

Migration strips volatile tokens into evidence and drops pure-status sentences, then keeps the rule. It also archives a task step tied to a PR or Project: after stripping, the decision is an imperative step (fix, address, resolve, run, rerun, merge, land, finish, hold) with a PR or Project id in it, or with before merge, before accepting, findings, or re-review, and it carries no rule word (must, never, always, only, when, because, rule). Stripping leaves no stray dash, empty brackets, or doubled tokens, and evidence lists each token once. Otherwise it archives only when nothing durable remains (the decision surface is empty or still a pure status line, or a required field is blank). An archive copies the record text into `data/archive/<date>.json` and never deletes the row. The script refuses a live root (`~/.context-ledger`, `~/.agents/plugin-data/context-ledger`, or `CONTEXT_LEDGER_DATA`), including a `--data` dir whose `binding.json` points at that ledger.

## caller_scope

`find` hard-filters by `caller_scope` `{project, tools}`. The allowed set is always fleet, plus `project:<project>` when a project is set, plus `tool:<id>` for each tool.

`find` with no `caller_scope` allows fleet only. It does not return every record.

`scripts/ledger_loop.py find` reads `binding.json` and passes `caller_scope`. The project is `binding.project`, else `project` in `data/project-registry.json`. `scope_id` is never used as a caller project. With no project the find is fleet plus `binding.tools`. The helper `find` in `skills/context-ledger/scripts/lookup.py` does the same from the open store binding.

`record` on the MCP path refuses a body with no `scope`. An owner write (`mcp=False`) may still insert a legacy row with no scope so migration can classify it. The helper sets `fleet` when it writes a candidate or a gap and the caller did not name a narrower scope.

## Fixture outcomes

These ids are classification cases, not personal notes.

| Id | Result |
| --- | --- |
| `2599cee1` | `fleet` or `tool:comet`, never `project:irctc` |
| `41badbdc`, `77b5eb43`, `cb291f09`, `d81dd34c`, `f06e057d`, `d897c824` | `project:open-resource-network` unless the text gains a fleet cue or a `fleet` entity (their stored text names ORN and carries no fleet cue) |
| `4f7f4626`, `e47b091b`, `7ab0366c`, `fd157e72` | stay active, volatile token moved to evidence |
| `3bfca7a8` | stay active (a model name is not a pin) |
| `4bafacf3`, `a4abc29c` | stay active |
| `4e5d0bad` | archive (a task step tied to a PR) |
