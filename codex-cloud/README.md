# Codex Cloud setup

Use this branch in a Linux Codex Cloud environment with Python 3, Git, the Codex CLI and network access for Python dependencies. The setup installs **Batched Change List** and **Developer Observability** from this checkout. It does not install desktop-specific plugins, modify project instructions, initialize a Context Ledger, or grant commit, push, publishing, or messaging authorization.

```sh
git clone --branch codex-cloud --single-branch https://github.com/ingpoc/agent-plugins.git
cd agent-plugins
python3 codex-cloud/install.py
```

The checkout is the registered local marketplace: keep it available while using the installation. Re-running the installer checks the existing marketplace location, installs the plugins again, and runs the observability runtime installer, which preserves an existing config. If the marketplace name already points elsewhere, setup stops rather than replacing it. `--context-ledger` also installs that plugin, but neither creates nor binds a ledger. Follow its installed instructions and use an existing owner-authorized ledger; do not copy a personal ledger or invent authorization.

Confirm with `codex plugin list`; open a fresh Codex session if the current session does not discover the plugins. Plugin installation in the CLI does not guarantee that a cloud chat host exposes their native tools.

## Cloud helpers

```sh
python3 codex-cloud/project.py preflight
python3 codex-cloud/cloud_health.py
python3 codex-cloud/project.py summary /absolute/path/to/live-findings.json
python3 codex-cloud/project.py diagnose PROJECT_ID --project-dir /absolute/path/to/project
```

The health check reports preview status and searches the workspace and plugin cache for the Sites source helper. It does not start services or read logs; desktop state and relay failure causes remain unknown.

Use the live path returned by `bcl --data FINDINGS.json status`; the summary cannot determine whether an arbitrary file is current. The diagnosis may run the supported collector once if the snapshot or project is missing or stale. Collection can move expired plugin-owned files to Trash. Empty findings are not proof of a passing test.

If native Observability tools are absent, the MCP bridge is available with the installed runtime:

```sh
~/.local/share/developer-observability/runtime/bin/python3 codex-cloud/observability.py list
~/.local/share/developer-observability/runtime/bin/python3 codex-cloud/observability.py observability_diagnose '{"include_coverage":true}'
```

Read the installed plugin skills before use. Batched Change List stages can start viewers and commit snapshots; use them only within the user's authorization. Its live viewer binds to loopback, not the public internet. Never put secrets, private evidence, ledger databases or local configuration into this repository.

## Preview attempt evidence

When the Sites workflow permits a preview start, record an attempt with:

```sh
python3 codex-cloud/preview_attempt.py start /absolute/path/to/site --project PROJECT_ID --issue ISSUE-01
```

This command **starts the managed preview**. Do not repeatedly retry a failed preview without a concrete repair or evidence of recovery. It saves a private, permission-restricted JSON record under `~/.local/state/codex-cloud/preview-attempts/`; use `--record-dir` to choose another private location. The record contains a generated ID, time, project ID, bounded phase and error code, exit code, and optional reference IDs. It does not save raw command output, a site path, or the preview URL. On success the exact URL is returned in the command response, not saved in the record. The command exits nonzero if preview startup fails.

To inspect a record and check an explicitly supplied tracker reference:

```sh
python3 codex-cloud/preview_attempt.py diagnose /absolute/path/to/attempt.json --tracker /absolute/path/to/live-findings.json
```

`--finding FINDING_ID` on `start` records an optional Developer Observability finding ID. The diagnostic checks that ID against a current project snapshot. References are user-supplied leads, not proof that a finding caused the attempt failure. Missing, stale, unmatched, or unavailable evidence cannot establish health or cause. The recorder can classify the documented relay-readiness timeout, but the managed service must provide additional diagnostic data before deeper root cause can be established. Do not commit attempt records or private logs.
