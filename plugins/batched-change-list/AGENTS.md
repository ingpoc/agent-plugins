# batched-change-list — install

This directory is the portable [Agent Plugin](https://agent-plugins.org/specification). Load this folder (`plugin.json` here). Do not load the collection root. Do not add `.codex-plugin/`.

Collection routing: repo-root `AGENTS.md`. Client load path: [compatible-clients](https://agent-plugins.org/compatible-clients) → that client's setup page.

## What ships

Skills only. `skills/batched-change-list/` holds `SKILL.md`, four references, three stdlib Python scripts, one HTML template, an example tracker and their tests. There is no `mcp.json`, no `bin/`, no install script, no data directory and no network service except the optional loopback viewer on `127.0.0.1:53366`.

Requires `python3` (standard library only). Nothing else to install or configure after the client loads the plugin.

## After the client has loaded this package

1. Confirm the skill is listed: `batched-change-list`. If the client lists plugins but not their skills, its skill discovery is off; fix that in the client, not by copying this folder elsewhere.
2. Put the tracker in the product under test, not in this plugin: for example `<project>/docs/change-list/findings.json` with the rendered `change-list.html` beside it (`skills/batched-change-list/examples/findings.json` is the shape). Commit both with the fixes they describe.
3. Self-validate the package after any edit:

```sh
cd skills/batched-change-list
python3 -m unittest discover -s scripts -p 'test_*.py'
```

4. If the client installs a copy outside this folder, that copy is the one the agent loads. Refresh it after a version bump; never edit the installed copy.

## Merge authorization (owner opt-in)

This package ships **no** standing authorization to merge. With nothing recorded here, the skill stops after a verified-fix report and asks before merging, publishing, installing or sending anything.

An owner who wants the original hands-off rule records it in the product repository’s applicable agent instructions, for example its `AGENTS.md`:

> Standing authorization: when at least 10 distinct findings in the current PR are `verified_fixed`, finish accessible verification, commit and push the verified-fix report, and merge to `main` after required checks and reviews pass. Verify the merge, then fast-forward the local `main` without overwriting local changes. Explicit instructions and project restrictions override this.

The skill checks the applicable product repository instructions for this policy. Add it only for a repository where an autonomous merge of a fix PR is acceptable; do not edit the installed plugin copy.

Behavior: `skills/batched-change-list/SKILL.md`.
