# Release manifest follow-up validation

Date: 2026-10-31. Branch: `codex/release-manifest-hooks`.
Starting base: `0e2df56a7ccebef80f4ccd3a8ed112860f1164f1`.
ACU package/runtime metadata increased from 0.2.65 to 0.2.66; this is not a release.
No release, upload, tag, merge, push, runtime replacement, or harness installation
was performed. Upload execution tests use a mock runner.

## Changes

- `scripts/release_assets.py`, `scripts/test_release_assets.py`: DMG inspection,
  schema-1 manifest, self-contained assets and checksums, dry-run-default upload.
- `scripts/install-mac.sh`, `scripts/test_install_mac.py`: pinned verification,
  install/finish/remove, structured summaries, local-file failure-path tests.
- `docs/install-mac-SPEC.md`: exact handoff copy, unchanged.
- `plugins/agent-computer-use/bin/acu`, service `Sources/CUAService/Status.swift`,
  `main.swift`, and `Tests/CUAServiceTests/StatusTests.swift`: read-only status.
- ACU `hooks/hooks.json`, `hooks/cursor-hooks.json`, `.cursor-plugin/plugin.json`,
  `.claude-plugin/plugin.json`, `.claude-plugin/mcp.json`, `bin/acu-claude-hook`,
  and root `.claude-plugin/marketplace.json`: separate harness hook contracts,
  Claude package registration, and fallback deduplication.
- ACU `plugin.json`, `.grok-plugin/plugin.json`, service `Resources/Info.plist`,
  `CHANGELOG.md`, `README.md`, and `scripts/release_agent_computer_use.py`:
  aligned 0.2.66 metadata, version gate, release/proof documentation.
- `scripts/check.py`, ACU `test_plugin_package.py`, `test_claude_hook.py`,
  `test_prompt_lint.py`, and context-ledger `tests/test_contract.py`: update
  affected package, catalog, and read-only Keychain invariants. The catalog test
  now derives Claude packages from actual shipped manifests.

## Acceptance results

Failure signals: nonzero exit unless an expected rejection code is specified;
invalid/missing JSON is independently a failure.

| Check | Exit | Observed output / result |
| --- | --- | --- |
| `~/.local/bin/shellcheck scripts/install-mac.sh` | 0 | No diagnostics |
| Real 0.2.64 DMG asset build | 0 | Manifest version `0.2.64`, build `450`, bundle `com.ingpoc.cua-service`, minimum macOS `13.0` |
| `shasum -a 256 -c SHA256SUMS` in asset directory | 0 | DMG, installer, manifest: `OK` |
| Fresh dry-run with real 0.2.64 DMG and local manifest | **10** | `App signature failed.`; JSON parses; signature rejection described below |
| Tampered manifest checksum | 10 (expected) | `Manifest checksum mismatch.`; JSON parses |
| Ad-hoc-signed tiny app DMG | 10 (expected) | `DMG Gatekeeper verification failed.`; download moved to Trash; JSON parses |
| Up-to-date installed 0.2.64 / build 450 rerun | 0 | Runtime `skip`, path `/Applications/Agent Computer Use.app`; JSON parses; not every harness action is skip |
| `python3 scripts/check.py` | 0 | `check ok`; opt-in integration suites skipped |
| `swift build` in service package | 0 | Build complete |
| `swift test` in service package | 0 | 391 tests, 12 skipped, 0 failures |
| Release asset unit tests | 0 | 8 passed |
| Installer boundary tests | 0 | 3 passed |
| Claude hook tests | 0 | 20 passed |
| ACU package/manifest tests | 0 | 7 passed |
| `claude plugin validate .` | 0 | Validation passed with missing-marketplace-description warning and README advice |
| `claude plugin validate plugins/agent-computer-use` | 0 | Validation passed; README install advice |
| `workflow lint` | 0 | 0 semantic errors; existing global AGENTS word-budget warning |
| `git diff --check` | 0 | No diagnostics |

The source DMG was read-only at `~/code/acu-release/dist/AgentComputerUse-0.2.64.dmg`.
Its SHA256 is `4fca0d046598deb30e3bfcacd846741bbd21e2ea5f908a9a652b6dc64b21c7bb`.
DMG Gatekeeper assessment passes, but mounted-app strict verification reports:

```text
Agent Computer Use.app: resource fork, Finder information, or similar detritus not allowed
App signature failed.
```

The downloaded copy is detached and moved to Trash; the original remains
untouched. The required fresh-install exit-0 acceptance is therefore **blocked
by the source artifact**. The installer does not weaken or bypass this check.
The ad-hoc fixture is rejected by the earlier DMG Gatekeeper check, so that test
does not independently exercise the later TeamIdentifier comparison.

The up-to-date rerun reports runtime and Codex plugins as `skip`. Cursor entries
are `would-manual`, Claude entries are `would-install`, and Grok entries are
`handled-by-bot`. Accordingly, the requested **every action skip** condition is
not met on this machine. No missing harness installation was performed to make
the result green.

A source-built `CUAService status --json` exited 0 and parsed successfully:
`version=dev`, `build=unknown`, `running=true`, all three permissions granted,
`island.state=locked`, `ok=true`. This executes the built command; it does not
prove that the installed 0.2.64 app contains the new command. That app was not
replaced. Debug executables correctly lack release-bundle metadata.

## Remaining integration points

- Obtain a corrected signed/notarized artifact to satisfy the fresh dry-run
  exit-0 acceptance without changing verification policy.
- Resolve missing/manual harness setup and Grok's bot-owned live proof. Codex
  and Claude CLI checks prove an enabled versioned plugin receipt; Cursor's
  check enumerates live MCP tools. These are labelled in the README and summary.
- The inherited gate store has no separate grace state. Status reports
  `unknown` for unverifiable/expired entitlement evidence, and never prompts.
- During this task, `codex/island-gate` advanced to
  `b10ba4fcb654285cf92d1ac4d3b56d386ba1d487`. This follow-up keeps its original
  base; it has not merged or rebased that concurrent change.

Independent documentation review: pass for README/implementation consistency
under the user's explicit manual-install exception; handoff spec preserved.
Context Ledger access check succeeded, but the requested installed lookup helper
was absent; lookup stopped without a retry or retrieved guidance.
