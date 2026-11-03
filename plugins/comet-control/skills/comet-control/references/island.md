# Lease on the Dynamic Island

The Comet Control broker mirrors every active lease onto the Agent Computer Use (ACU) Dynamic Island, so Guru can see which agent is using Comet. Agents do nothing extra.

## What shows

- One row per lease: Comet icon circle, name = the lease's `agentLabel` (else `agentId`, else `Agent`, max 48 chars), title `Using Comet`, meta `comet-control · <age>`.
- Pass a short human `--label` (the lease `agentLabel`) at `start` (for example `smallcase-manager` or `Grok Bot · bookings`). Never put secrets, URLs with tokens, or user data in it.

## Lifecycle

- The broker thread (`plugin/comet_control/native/island_mirror.py`) reads the token-free `sessions` inventory every 5 s and posts to ACU's owner-only `island.sock` with source `agent` and id `comet-lease:<session_id>`.
- Open lease → row within ~5 s. Closeout or extension TTL reap → row cleared within ~5 s.
- Heartbeat: the row is re-posted every 20 s with `ttl_s` = lease time left + 15 s. If the broker dies, the island marks the row **updates stopped** after that TTL; the next broker start clears it from `run/island-rows.json`.
- Comet closed or extension gone for 3 polls in a row → all lease rows clear.
- `lease_token` is never read into a row or logged.

## Requirements and opt-out

- ACU running, Settings → AI Agents → **Agent Roster** enabled and set up (Rotate). Without ACU the mirror is a silent no-op.
- `COMET_CONTROL_ISLAND=0` in the broker's environment turns the mirror off.
- Manual check: `python3 plugin/comet_control/native/island_mirror.py --socket run/comet-control.sock --state run/island-rows.json --once` prints `{"active": n, "posted": n, "cleared": n}`.
