# Lease on the Dynamic Island

The Comet Control broker mirrors every active lease onto the Agent Computer Use (ACU) Dynamic Island as a **plugin mark**: a circle on the right of the compact pill, tied to the agent driving the lease. Agents do nothing extra. This needs ACU installed with its shared `bin/island_activity.py`; without it the mirror is a silent no-op.

## What shows

- Right of the pill: the comet-control logo circle (after Samantha's app circles). Inside the pill's lead slot: Comet's icon in a spinning ring (what the plugin controls). The left side stays the agent harness circles.
- Tooltip `Comet Control · <agentLabel>`; VoiceOver `Using Comet Control: <agentLabel>`. The label goes through ACU's shared name normaliser (`smallcase-manager` shows as "Smallcase Manager").
- Pass a short human `--label` (the lease `agentLabel`) at `start` (for example `smallcase-manager` or `Grok Bot · bookings`), max 48 chars. Never put secrets, URLs with tokens, or user data in it.
- Agents using Comet group into one circle: two labelled leases show one Comet circle naming both agents.

## Lifecycle

- The broker thread (`plugin/comet_control/native/island_mirror.py`) reads the token-free `sessions` inventory every 1 s and posts through ACU's `island_activity.py` with source `agent`, plugin `comet-control`, and row id `plugin:comet-control:<session_id>`.
- Open lease → row within ~1 s. Stays while the lease is open. Closeout or extension TTL reap → the row lingers 2 s, then drops (fade 200 ms).
- Heartbeat: the row is re-posted every 20 s with `ttl_s` = lease time left + 2 s (min 30 s). The hub **drops plugin rows at that TTL** — they never go "updates stopped" — so a dead broker leaves no stale row. The next broker start clears anything left in `run/island-rows.json`.
- Comet closed or extension gone for 3 polls in a row, or the broker stops → all lease rows linger 2 s and drop.
- `lease_token` is never read into a row or logged.

## Requirements and opt-out

- ACU running with `bin/island_activity.py`, Settings → AI Agents → **Agent Roster** enabled and set up (Rotate). Without ACU the mirror is a silent no-op.
- `COMET_CONTROL_ISLAND=0` in the broker's environment turns the mirror off.
- Manual check: `python3 plugin/comet_control/native/island_mirror.py --socket run/comet-control.sock --state run/island-rows.json --once` prints `{"active": n, "posted": n, "cleared": n}`.
