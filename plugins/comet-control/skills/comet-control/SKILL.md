---
name: comet-control
description: "Use when an agent must control or verify web apps in an isolated visible Comet window without touching the bundled Chrome extension or Chrome profiles."
---

# Comet Control

> **Self-validate after edits.** Run `./skills/comet-control/scripts/validate.sh --strict` from the plugin root after changing this skill or its references.

One leased visible Comet window per agent. No Chrome, shared-window, or headless control. API-first for known JSON or file data. Never expose lease tokens because they authorize control.

| Step | When | Reference | Script |
|---|---|---|---|
| Choose and prepare | Choosing API-first, probing, installing, or starting a lease | [critical-path.md](references/critical-path.md) · [api-first.md](references/api-first.md) · [fast-path.md](references/fast-path.md) · [extension-install.md](references/extension-install.md) · [optimize.md](references/optimize.md) · [comet-capabilities.md](references/comet-capabilities.md) | `scripts/durable_lease_controller.py`; `scripts/lease_driver.py` |
| Operate | Acting, observing, recovering locators, or using advanced browser controls | [speed-bar.md](references/speed-bar.md) · [observe-feedback.md](references/observe-feedback.md) · [operate.md](references/operate.md) · [locator-misses.md](references/locator-misses.md) · [advanced.md](references/advanced.md) · [debugger-blocked.md](references/debugger-blocked.md) · [browser-use.md](references/browser-use.md) · [devtools.md](references/devtools.md) · [wrapper-skills.md](references/wrapper-skills.md) | `scripts/form_plan.py`; `scripts/browser_use_cdp_bridge.py` |
| Handoff | OS sheets, OAuth, another window, or another agent owns control | [native-coexistence.md](references/native-coexistence.md) · [multi-agent.md](references/multi-agent.md) · [google-accounts.md](references/google-accounts.md) · [razorpay-bank-popup.md](references/razorpay-bank-popup.md) · [secure-copy-keychain.md](references/secure-copy-keychain.md) · [agent-handbook.md](references/agent-handbook.md) | |
| Close out | Closing a lease or checking its Dynamic Island row | [island.md](references/island.md) | |
