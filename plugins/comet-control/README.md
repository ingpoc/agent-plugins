# comet-control

Drive web apps through one leased visible Comet window. Use when an agent must control or verify pages in Comet without touching Google Chrome or Chrome profiles.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: [`AGENTS.md`](AGENTS.md). Collection routing: repo-root `AGENTS.md`.

## Overview

**Purpose.** Gives each agent one leased, visible Perplexity Comet window. Agents drive and verify web apps through it, and other tabs, other agents, and Google Chrome stay untouched. A local broker (`plugin/comet_control/native/broker.py`) pairs with an unpacked Comet extension (`plugin/comet_control/extension/`). Agents call the lease controller (`skills/comet-control/scripts/durable_lease_controller.py start|send|status|closeout`) as the `comet-control` skill describes. There is no MCP server.

**Requirements.** macOS, [Perplexity Comet](https://www.perplexity.ai/comet) installed and opened at least once, and `python3`. The one-time extension load uses the `agent-computer-use` plugin, or you can do it by hand. The tests also need `websockets==16.0` and `Pillow`.

**Install.**

1. Install the `comet-control` plugin from this repository's marketplace.
2. Load the extension once. In Comet, open `chrome://extensions`, turn on Developer mode, click **Load unpacked**, and pick `plugin/comet_control/extension` inside the installed plugin. [`references/extension-install.md`](skills/comet-control/references/extension-install.md) has the agent-driven version.
3. From this directory, run `./scripts/ensure-broker.sh start` and then `./scripts/ensure-broker.sh probe --json`. The probe must report `success: true`, `runtime_verified: true`, and `extension_connected: true`.

**Configuration.** Nothing is required. Runtime state lives under the plugin's `run/` directory, or under `~/.agents/plugins/comet-control` when it is installed there. `COMET_CONTROL_BRIDGE_SOCKET` overrides the broker socket path.

**Network endpoints, permissions and credentials.** The broker listens only on a Unix socket and on `ws://127.0.0.1:38927` for the extension. It calls no remote endpoint. The extension requests `debugger`, `tabs`, `scripting`, `webNavigation`, `history`, `downloads`, `clipboardRead`/`clipboardWrite` and `<all_urls>`, because it must drive whatever site the agent leases. It acts only in the leased window. The plugin stores no credentials. The optional secure-copy flow ([`references/secure-copy-keychain.md`](skills/comet-control/references/secure-copy-keychain.md)) reads a named macOS Keychain item at run time and never prints it.

**Testing.** From this directory: `python3 -B -m unittest discover -s plugin/comet_control/tests -q` and `./skills/comet-control/scripts/validate.sh --strict`. For a live check, see the reviewer steps in the repository's `docs/marketplace-submission.md`.

**License.** MIT (repository root [`LICENSE`](../../LICENSE)).

## Benchmarks

This plugin has no 0–10 `run_benchmarks.py` suite yet. Scores stay **unmeasured** until a grader writes them. Live proof is the test files below — pass/fail, not a 0–10 fill. Do not invent scores.

| Axis | Score |
| --- | --- |
| Reliability | unmeasured |
| Robustness | unmeasured |
| Context efficiency | unmeasured |
| Speed | unmeasured |
| Efficiency | unmeasured |

### Reliability

Evidence, not a score: `plugin/comet_control/tests/test_dashboard_interactions.py` (click + cursor-at-target). Isolation: `test_multi_agent_isolation.py` must exit 0 on two consecutive runs.

### Robustness

Evidence: lease token private to one process; SPA remount recovery in `skills/comet-control/references/optimize.md`; broker fails closed unless the logged-in Comet runtime is connected. First install / reload of the unpacked extension is owned by `skills/comet-control/references/extension-install.md` (Agent Computer Use on `chrome://extensions`, then probe + smoke).

### Context efficiency

Evidence: `page_context` compact; diagnostics opt-in (`test_developer_diagnostics.py`); `network_summary` stays bounded.

### Speed

unmeasured. No official duration floors.

### Efficiency

Evidence: one durable `durable_lease_controller` per campaign; short-lived shells that drop the lease token are rejected as an antipattern in `SKILL.md`.
