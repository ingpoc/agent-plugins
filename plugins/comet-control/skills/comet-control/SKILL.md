---
name: comet-control
description: "Use when an agent must control or verify web apps in an isolated visible Comet window without touching the bundled Chrome extension or Chrome profiles."
---

# Comet Control

Editing this skill? Read [principles.md](principles.md) first so edits pass it.

One leased visible Comet window per agent. No Chrome, shared-window, or headless control. API-first for known JSON or file data. Never expose lease tokens because they authorize control.

| Step | When | Reference | Script |
|---|---|---|---|
| Choose and prepare | Choosing API-first, probing, installing, or starting a lease | `references/critical-path.md`, `references/api-first.md`, `references/fast-path.md`, `references/extension-install.md`, `references/optimize.md`, `references/comet-capabilities.md` | `scripts/durable_lease_controller.py`; `scripts/lease_driver.py` |
| Operate | Acting, observing, recovering locators, or using advanced browser controls | `references/speed-bar.md`, `references/observe-feedback.md`, `references/operate.md`, `references/locator-misses.md`, `references/advanced.md`, `references/debugger-blocked.md`, `references/browser-use.md`, `references/devtools.md`, `references/wrapper-skills.md` | `scripts/form_plan.py`; `scripts/browser_use_cdp_bridge.py` |
| Handoff | OS sheets, OAuth, another window, or another agent owns control | `references/native-coexistence.md`, `references/multi-agent.md`, `references/google-accounts.md`, `references/razorpay-bank-popup.md`, `references/secure-copy-keychain.md`, `references/agent-handbook.md` | |
| Close out | Closing a lease or checking its Dynamic Island row | `references/island.md` | |
