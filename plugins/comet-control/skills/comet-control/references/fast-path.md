# Comet Control — Fast path & recovery

Run from the runtime root. If the claim is a known JSON/file shape, stop — do not probe or lease.

## Start

```bash
./scripts/ensure-broker.sh probe --json
# require success + runtime_verified + extension_connected

WORK=/tmp/comet-control-$SESSION_ID
python3 skills/comet-control/scripts/durable_lease_controller.py start \
  --session-id "$SESSION_ID" \
  --label "<short agent label>" \
  --url "https://example.com/" \
  --workdir "$WORK" \
  --ttl-seconds 600
# wait: ready.json with ok:true + lease_ready_at
```

One controller for the whole campaign. `controller_not_alive` is not a remint —
if `_run` pid is live, `start` restores `controller.alive` (`controller_already_running`) then `send`.
Do not invent a second session id, FIFO wrapper, or raw `request.json` writes.
Process-local lease token dies with short `python3 -c` shells → orphan window; keep one durable controller.

## Drive (one path)

```bash
python3 skills/comet-control/scripts/durable_lease_controller.py send --workdir "$WORK" \
  '{"actions":[{"type":"page_context"}]}'
python3 skills/comet-control/scripts/durable_lease_controller.py send --workdir "$WORK" \
  '{"actions":[{"type":"click_text","text":"Continue"},{"type":"page_context"}]}'
```

Batch coherent slices. Prefer semantic locators before coordinates. Visible cursor only —
never script-eval clicks. Visual claims require a `screenshot` action and reading the file.
`handoff_hint` / in-page Google CTA while overlay is up → `cua_slice` on the **same** lease
([`multi-agent.md`](multi-agent.md)); do not remint.

SPA remounts: separate sends (click → wait → `page_context`). Default `--timeout` 180 is for
known remounts; use 30–60s for simple reads. JS `prompt`/`confirm`/`alert`: batch `dialog_handle`
(+ `promptText`) — see [`advanced-capabilities.md`](advanced-capabilities.md).

Optional Browser Use CDP: start with `--browser-use` and load [`browser-use.md`](browser-use.md).
**Never** run Browser Use CLI and `send` as concurrent input owners.

## Closeout

```bash
python3 skills/comet-control/scripts/durable_lease_controller.py closeout --workdir "$WORK"
```

Once at terminal boundary. Require `verified_absent: true`. Failed command ≠ boundary.

## Recovery

```bash
./scripts/ensure-broker.sh start
./scripts/launch-comet.sh
./scripts/ensure-broker.sh probe --json
```

`EXTENSION_NOT_CONNECTED` / reload unpacked → [`extension-install.md`](extension-install.md).
Speed / observe thrash → [`speed-bar.md`](speed-bar.md). Typed failures → [`observe-feedback.md`](observe-feedback.md).
