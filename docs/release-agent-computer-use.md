# Agent Computer Use release gate

Owner: `scripts/release_agent_computer_use.py`. Run from the accepted worktree.
The required Origin `scripts/check.py` check and GitHub mirror CI both call its
source policy. Neither CI path installs unreviewed PR code on the working Mac.

## Source gate

`python3 scripts/release_agent_computer_use.py --check`

The same gate runs through `scripts/check.py`: package, Cursor override and app
versions must agree; shipped package changes require a version increase over
`origin/main` in CI, or HEAD locally. Tests alone need no version increase.
CI needs the base history; missing history is an error. Set
`AGENT_PLUGINS_RELEASE_BASE` to an explicit base commit when comparing elsewhere.
The canonical collection runner rejects empty suites and unlisted skips.

## Full release

`python3 scripts/release_agent_computer_use.py`

Requires a clean committed worktree. The full release always compares package
changes against `origin/main` (or the explicit release base), including changes
already committed locally. One machine-wide lock rejects concurrent releases. Runs source checks, builds and installs the
notarized app, syncs the installed source through its owner, refreshes Codex,
checks source/app/service identity and signature, runs the three live integration
suites, and proves a fresh MCP handshake and independent Calculator readback.
The microphone preflight must pass. At the voice prompt, turn Samantha ON and
say “Open Calculator and calculate thirteen times seventeen.” Within 90 seconds
the gate requires fresh input, a successful Calculator action and a completed
response from one session, no disconnect/error, live audio gateway health, and
independent `13 × 17 = 221` readback. This uses the configured paid voice service;
only run the full gate when that usage and foreground Mac interaction are intended.

The command never turns Samantha on. It leaves a successful voice session on;
turn it off after testing if no longer needed. An already-open chat may retain a
stale MCP process: the fresh process check proves installation, not that chat's
reconnection. Stale clients remain blocked by the service contract.

Evidence defaults to `~/.agents/artifacts/acu-release.json`, with per-step logs
beside it. Evidence must live outside the worktree. Any failed step exits nonzero
and writes `release_ready=false`. Source changes during execution invalidate the
run. The receipt records the commit, worktrees, package identity, installed path,
step outcomes, signing status and sanitized voice evidence; it omits transcripts
and secrets. Receipts are local evidence, not authenticated remote attestations.

For a local signed test, add `--sign-only`. It never produces distribution-ready
evidence. To validate the existing app without rebuilding or restarting it, add
`--verify-installed`; this still refreshes Codex through its plugin installer and
runs the source/live/voice gates. This mode requires the installed package to
match the source. `--report /absolute/path.json` selects a separate evidence file.

## Publication boundary

Do not tag or publish an ACU release without a successful distribution-ready
receipt matching its exact commit and package identity. The command does not
publish or merge. The forge's source check blocks invalid source changes;
Mac-side proof is required separately because Linux CI cannot establish it.
