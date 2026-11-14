# Viewer

Every `bcl` call ensures a per-list viewer. It starts or restarts `serve.py --data <live-file>` on an available loopback port and records port, pid, plugin version, data path, and started-at in `$BCL_STATE_DIR/viewers/<list-id>.json`. The viewer always serves the live file, not a stale in-repo snapshot.

The live page footer shows `reading <path>, viewer <version>, started <IST>` only when the viewer is live. Default `render.py` output stays deterministic and has no footer. Times on the page are IST; never leave visible ISO timestamps in the user view.

The page polls with `HEAD` and reloads only when the ETag or fingerprint changes. That interval is a change check, not a full-reload timer. Local-file views still require a manual refresh.

The default bind port is 53366 when it is free and not already reserved by another list; if that port already serves this exact list, reuse it. If another report or service occupies the port, report the conflict and preserve it; do not stop another owner or silently switch that owner's port. For a new PR, update the served report only after verifying ownership. Keep the viewer running during the active run.

Recovery, if `bcl` cannot start the viewer: from the skill directory, `python3 scripts/serve.py --data /absolute/path/findings.json --port <free-port>`. Without `--inventory`, each request discovers installed plugin manifests and uses the current time. `--inventory /absolute/path/inventory.json` supplies an inventory override. For one-off renders, `render.py --live` discovers installs and uses the current time, while `--now <ISO-8601>` injects a deterministic render time.
