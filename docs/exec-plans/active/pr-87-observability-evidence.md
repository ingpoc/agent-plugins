# PR #87: Make Samantha evidence conclusive

## Purpose and acceptance

An agent investigating an installed Samantha test can identify the task, installed build, actions, Ledger decisions, timing, and independently observed result. It can tell a failure from missing evidence without inferring completion from a successful tool call. The canonical issue list is `docs/triage/pr-87-findings.json`; the rendered page is `docs/triage/pr-87.html`. Keep all existing fixed statuses until a new failure is reported or reproduced.

## Current state and ownership

- **OBS-10 / OBS-06:** `plugins/agent-computer-use/runtime/voice-cua/python/voice_cua/realtime_session.py` maps task, request and response IDs; `activity_log.py` emits bounded task outcomes. Tool-reported results remain unverified without independent proof of the requested postcondition.
- **OBS-12:** `ledger_helper.py` already performs lookup, recall and preference saves; `activity_log.py` records operation, status, count and bounded opt-in result/error detail. The existing payload may be truncated or omit nested result data; selected decision, application and supersession are not independently established. Same scope and rule supersede an active user decision; general captures do not automatically supersede.
- **OBS-13:** `plugins/agent-computer-use/skills/macos-cua/service/Sources/CUAService/IslandLayoutLog.swift` already records change-triggered geometry. `plugins/developer-observability/runtime/collector/sources.py` and `analyze.py` project individual layout occurrences. This does not yet prove a complete transition or final position.
- **OBS-14 / OBS-16:** the existing collector (`runtime/collector/sources.py`, `analyze.py`) and read side (`runtime/backend/observability_api.py`, `runtime/server.py`) already provide paged session timelines, opaque references, opt-in diagnostics and compact session counts. The task-level join and summary are the missing pieces. Reuse this pipeline rather than adding another store.
- **OBS-15:** ACU microphone preflight and audio readiness already emit stages, but their device state and recovery are not joined. AGENT-04 owns the product symptom.
- **AGENT-06:** the running MCP process pins its package identity at startup. The refreshed service correctly rejects that stale client. A safe handoff must replace old code with a package matching the live service before dispatch, preserve the current request and any buffered input across replacement, retain protocol/client state, and never retry an action that may already have run. An isolated two-message pipe confirmed that Python's buffered stdin can hold the next request, so a naive `exec` loses it. Test queued requests, both supported framings, unavailable or mismatched replacement, and at-most-once action delivery before installing and testing one persistent client across a subsequent refresh.

All paths above are under `plugins/developer-observability/` or `plugins/agent-computer-use/` unless fully qualified. The repo is public: keep real preferences, raw logs, credentials, and personal case data out of committed fixtures, the public HTML and PR text. User-authorized local diagnostics may expose actual Ledger outcomes and bounded raw error context on explicit request; continue credential redaction and label truncation. Do not claim general decision supersession where no link exists.

## Ordered implementation

### Preference retrieval and automatic use (USER-02, USER-03, OBS-06, OBS-10, OBS-12, OBS-14)

**Goal.** When a site or app presents a preference-sensitive choice, Samantha checks the relevant existing decision before asking, applies one unambiguous active preference, and reports the result accurately. A durable owner-stated correction is saved to the same decision point and independently readable later. No reminder to consult the Ledger is needed.

**Evidence and limits.** The 0.2.102 voice test shows an initial profile question without a Ledger call, then an explicit recall with zero results despite an existing active site-scoped preference. A later save used the browser scope while older preferences use the site scope. Controlled reads show exact scope/rule variants can return different results; the failed live call's actual scope and rule were not recorded, so do not assume which mismatch caused it. `providers.py` lets Live answer brief clarifications without delegation, while `__init__.py` puts proactive Ledger guidance in the backend. The live test also reused a run reference across distinct requests. The canonical issue status and evidence remain in `docs/triage/pr-87-findings.json`.

**Boundaries.** Change the Samantha-owned decision and routing path first: `runtime/voice-cua/python/voice_cua/{providers.py,realtime_session.py,tools.py,ledger_helper.py,activity_log.py}`. The Context Ledger package owns scope, ranking and supersession (`plugins/context-ledger/context_ledger/{contracts.py,store.py}`); change it only if a focused contract test proves its existing APIs cannot satisfy the behavior. Developer Observability owns projection and presentation (`plugins/developer-observability/runtime/{collector,backend}`); reuse existing storage and opaque references. Comet remains the browser owner. Do not write real preference values, transcripts, raw logs or decision IDs into public fixtures, the report or PR text.

**Acceptance and edge decisions.**

- [ ] A fresh spoken preference-sensitive choice delegates a decision check before Samantha asks or selects; a generic clarification, greeting or simple arithmetic does not trigger an unrelated Ledger call. An explicit request to use a preference follows the same path. A prompt change alone is not proof: test the default voice-to-backend route and fail closed on an unobserved check.
- [ ] Identify the actual site or app from trusted task/lease context; the browser is the transport, not the site's identity. Define and test canonical site identity and migration of existing site/domain aliases before writing anything. Do not assume a domain string is interchangeable with an app name or broaden access to unrelated sites.
- [ ] Search active relevant records at the established scope without requiring the model to guess an exact rule. Distinguish zero matches, unavailable Ledger, malformed request, expired/superseded record, and multiple conflicting matches. Apply only one independently established, unambiguous active preference; ask the user when no applicable decision or unresolved conflict exists. Never assert no preference exists solely because a narrow lookup returned none.
- [ ] Preserve existing stored choices and general captures; same-scope/same-rule supersession is claimed only when independently verified. A deliberate correction updates the canonical decision point once; retries or interruptions must not duplicate writes. Do not silently merge or delete existing records. **Open design decision:** whether legacy browser-scoped records are eligible for a particular site requires an explicit, testable mapping; unrelated browser preferences must not be treated as site choices.
- [ ] Record privacy-safe lookup scope and rule presence, candidate count, decision selection/application state, and task/session/build references in the existing pipeline; avoid values and raw prompts. A retrieved record is not proof it was applied. Distinct user requests must not share an invented run ID; late, ambiguous and unjoined events remain unknown. Verify behavior with a separate page/app readback and correlate it to the test before declaring success.

**Execution sequence.**

Use one coherent batch and one issue-by-issue installed run per iteration. Check shared prerequisites once before the installed pass; carry forward a precise unchanged blocker without repeating a check that cannot run. A shared exercise may support several issues, but each issue needs its own outcome and acceptance evidence. Finish every accessible check before a new fix cycle. Keep the JSON current, render the HTML through the findings manager, and consolidate related report-only edits into the batch commit. Move superseded summaries to diagnostics while retaining history. Do not request the consolidated voice session until the installed candidate and input are ready. Source and installed checks remain distinct. After the pass, assess deletion, simplification, optimization, and automation in that order; measure comparable outcomes before claiming savings.

For relevant live requests and transitions, inspect the Dynamic Island visually as well as through telemetry: check icon order, spacing, clipping and whether the state is understandable. Capture before and after when a transition matters. Record concrete design findings in this same list; still images do not prove motion quality or continuous observation.

1. Reproduce the captured sequence with synthetic site and profile data: a preference already exists, an initial choice needs it, then a separate correction is saved. Test the Live delegation boundary, Ledger scope/rule resolution and the consumer's actual choice independently. Add regression cases for empty, wrong-scope, aliases, conflicting active values, stale records, repeated writes, interruption and different sites. Compare the current observed failure to the same path after the change.
2. Implement the smallest shared owner fix. Specify the decision-boundary signal and how it reaches the backend before acting; if the platform cannot guarantee that ordering, keep the behavior explicitly unproven rather than presenting prompt adherence as enforcement. Define site identity and record-selection semantics before migrating existing data; do not turn an uncertain alias into an automatic action.
3. Add bounded attribution and tests for distinct spoken turns, out-of-order delegation, tool continuations and unlinked outcomes. Extend the existing observability projection only for fields actually emitted. Test that missing or ambiguous links stay unknown and diagnostic data remains redacted.
4. Run focused owner checks, then freeze the candidate and run the repository gate once: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_ledger_helper.py tests/test_ledger_preferences.py tests/test_ledger_0285.py tests/test_ledger_observability.py tests/test_providers.py` from `plugins/agent-computer-use/runtime/voice-cua/`; run affected Context Ledger and observability suites if changed; run `ACU_ISLAND_ACTIVITY=0 python3 scripts/check.py` from the repo root. Bump each changed shipped plugin's SemVer and aligned manifests/runtime assertions. Record failed stages and rerun them after fixing their cause.
5. Update PR #87, verify the pushed head, then use `scripts/release_agent_computer_use.py` for the exact open PR head. Check signing, installed package identity, running client identity and fresh/previous-client behavior; retain a failed release receipt. Start a new verification run through the batched-change-list management script immediately after installation and finish the accessible issue pass before another fix cycle.
6. Collect observability before and after the controlled installed batch. Independently exercise a synthetic save, readback, correction and conflict where possible; then request one concise voice session only for genuinely inaccessible voice and subjective checks. Test the user's existing site preference without disclosing its value in public evidence; verify the selected profile independently. Keep USER-02/USER-03 failing or awaiting verification until applicable live acceptance passes.

**Recovery and spec probes.** Missing site identity, matching record, microphone, consent or independent readback blocks only its dependent check. A stale client must reconnect through the supported lifecycle; do not weaken runtime identity checks or hand-edit installed caches. For stateful/I/O edges, retries preserve one write, concurrent or late events cannot cross tasks, and unknown/truncated data remains unknown (explicit; behavior tests). For text/collection edges, canonical aliases and conflict rules are explicit and irrelevant records are excluded (backstop: generated distinct-site and ambiguous-match cases). Prohibitions: no fabricated preference or success, no unrelated-scope lookup, no public private-value leakage (explicit and judgment review). The legacy browser-scope migration and a dependable voice decision-boundary signal remain unresolved design gaps until proved in milestone 2.

- [ ] **1. Freeze the evidence contract (OBS-10, OBS-06).** Trace actual task lifecycle and callers. Define one task/run identity that survives multi-tool turns, explicit event-level app/build identity, and joins to response and call references. Distinguish `tool_verified` from independent `task_verified`; record expected and observed postconditions only when a verifier captured them. Cancellation, missing, late, duplicate, stale and cross-session events must remain unknown or unverified, never success. Extend the existing event schema and ensure old logs remain readable.
- [ ] **2. Implement the Ledger decision trail (OBS-12; USER-02, USER-03, AGENT-02).** Associate lookup and returned decision identifiers, selection/application evidence when actually observable, write receipts and supersession outcomes with the same task. Avoid inferring application from retrieval. Preserve complete user-authorized result/error details in the local opt-in path subject to bounded storage and credential redaction; expose truncation explicitly. Show concise status/count first. Verify exact-scope preference behavior and keep general captures distinct from user-decision replacement.
- [ ] **3. Join task stage timings (OBS-14; OBS-01–03).** Correlate voice input, model, Ledger, Comet, native action, response and playback with the task identity. Expose per-stage duration, timestamp, outcome and explicit unknowns; do not double-count overlapping or mirrored stages. A latency threshold is a diagnostic setting until a product target is agreed.
- [ ] **4. Add transition and device evidence (OBS-13, OBS-15; OBS-04, USER-01, USER-05, AGENT-04).** Record attributable island open/resize/close/exit and settled bounds, app or layout state, and input-device selection/availability at preflight and audio-ready. Correlate failure and recovery without claiming causality merely from succession. Bound snapshots and expose missing frames or device state.
- [ ] **5. Present a concise task view (OBS-16).** Extend the existing MCP and dashboard: task/check, installed build and attribution, status, duration, failed stage and evidence completeness first; expand action timelines, Ledger results, recorded errors and geometry. Collapse only demonstrably mirrored events; preserve underlying occurrences, counts, timestamps and provenance. Unknown or truncated data must remain visible. Preserve pagination and existing consumers.
- [ ] **6. Validate, install, and reconcile.** Add focused behavioral tests at the owner boundaries, including valid and missing joins, independent versus tool verification, Ledger save/replace/recall, pagination, deduplication, interrupted transitions, no-device then recovery, and backwards compatibility. Run the relevant plugin suites and `python3 scripts/check.py`. Bump each changed plugin's SemVer and aligned manifests/runtime assertions. Update the same PR before installing its exact code head; use the repository release/install owners and record the installed identities.
- [ ] **7. Perform one tracked installed pass.** Start a distinct installation verification run in the existing JSON; render each issue as checking before its check, then record fixed, not fixed, blocked or inconclusive. Independently verify a fresh app task and its expected/observed postcondition under one task ID. Use a controlled non-sensitive preference: save, independently read it back, replace it, verify the older active decision's state, then test automatic retrieval and application. Exercise island transitions, a browser-assisted task and microphone startup/recovery as available. Collect Developer Observability before and after with build and run identity. Add new issues immediately and complete the whole accessible pass before entering another fix cycle. Request user speech only where no equivalent agent-driven test can prove the interaction.

## Checks and evidence

- ACU targeted suites live under `plugins/agent-computer-use/runtime/voice-cua/tests/` (including `test_credential_capture.py`, `test_ledger_helper.py`, `test_ledger_preferences.py`, and `test_reconnect_comet.py`) and `skills/macos-cua/service/Tests/CUAServiceTests/` for island transitions.
- Developer Observability targeted suites live under `plugins/developer-observability/tests/` (including `test_session_timeline.py`, `test_occurrence_evidence.py`, and layout/accuracy tests). Test fresh MCP schema and real installed client separately from source tests.
- A fix is `verified_fixed` only after the applicable source tests and attributable installed/live acceptance pass. Keep blocked, inconclusive and untested distinct; retain all existing fixed labels unless new contrary evidence is observed. A full test-suite pass or an emitted task outcome alone does not establish an independent product result.
- The HTML's `Agent details` holds technical provenance and expandable diagnostics; the user view states issue, proposed fix, status and relevant cropped screenshots. Local live evidence can contain the authorized Ledger outcome and recorded errors; the committed report must not contain personal data.

## Recovery and decisions

- PR #87 remains the integration owner. Preserve dirty work and verify branch, remote head and installed source identity before each install. Do not merge or publish without a separate request.
- A new install starts a new unique verification run without removing existing fixed statuses. The tracking JSON is authoritative; if rendering fails, regenerate the HTML from it. Keep interrupted runs and outcomes instead of deleting evidence.
- If a stage, matching preference, task identity, device or independent verifier is unavailable, record the exact boundary and continue checks that do not depend on it.
- No implementation or live acceptance has been performed for OBS-12 through OBS-16 in this planning pass. OBS-05, OBS-07, OBS-08, OBS-09 and OBS-11 retain their existing fixed status for their narrower verified capabilities.

## Progress and decision log

- 2026-11-06: Added OBS-12–OBS-16 and extended OBS-06 and OBS-10 in the canonical list. This is an implementation plan, not evidence that the enhancements are complete.
- Current decision: prioritize task correlation and independent outcomes, then Ledger decisions; timing, island/device transitions and presentation build on the same identity contract. Measure any efficiency claim against a comparable baseline and acceptance cohort.

### Iteration review: 0.2.105

- **Delete:** No release or evidence step was removed: the required source and installed gates check different boundaries, and no comparable proof supports deleting either.
- **Simplify:** Moved the cross-plugin synthetic integration test to collection tooling, leaving the portable ACU package's own tests independent of a sibling repository layout. The installed 0.2.105 package subsequently passed its 76-test focused suite.
- **Optimize:** The release reran a full source gate after an earlier full gate, but the runs are not a validated interchangeable receipt. No speedup is claimed; any reuse would require a frozen-source identity, auditable receipt and fail-closed invalidation.
- **Automate:** The existing findings manager already starts and records installation runs. Do not add another tracker or bypass its start/finish checks. A potential future improvement is automatic installation-receipt validation and run creation, only after its owner and failure/retry behavior are proved.
- **Outcome:** The 0.2.105 release installed, but its microphone gate failed. The new 29-item run has explicit results; old-client refresh remains failing, the voice path is blocked, and the rotation fix still needs installed service concurrency and crash-recovery evidence. The 0.2.104 failed gate exposed a log-rotation defect, tracked as OBS-17. No completed source test is promoted to installed acceptance on its own.

### Iteration review: 0.2.106

- **Delete:** No verification or release stage was removed. The source and installed checks verify different artifacts, and the microphone failure is not an acceptance pass.
- **Simplify:** The task identity change retires a completed spoken ID in the existing provider; unmatched delegations remain unknown instead of adding a second correlation store. Source tests cover a stale-ID reproduction and a spoken tool continuation.
- **Optimize:** No comparable end-to-end measurement establishes a speed improvement. Repeated source gates and signing remain candidates for future measurement, not proof that a stage can be skipped.
- **Automate:** The existing findings manager records the installation and per-issue outcomes; no additional tracker or autonomous retry was introduced. Automated promotion of a caller-reported task outcome would be a false pass and remains prohibited.
- **Outcome:** ACU 0.2.106 installed from PR commit 603f5dd; 130 focused installed-package tests passed. The release failed at microphone preflight. The 29-item run remains two fixed, one not fixed, 14 blocked and 12 inconclusive. An already-running client rejected its stale runtime contract; task correlation and preference use still lack attributable spoken acceptance.

### Iteration review: 0.2.107

- **Delete:** Removed the 30-second Ledger lookup cache, which returned stale results after preference changes, including changes outside Samantha. The tradeoff is a Ledger find for every lookup; no latency improvement is claimed.
- **Simplify:** The release test now waits for its asynchronous shutdown callback instead of assuming it finished when the HTTP response arrived. The first release failed that test; the corrected source gate passed on the next pushed head.
- **Optimize:** The release ran a full source gate after an earlier full run; freeze the next candidate before its single required full release gate. Preserve focused checks during edits. No comparable time or token saving has been measured.
- **Automate:** The existing release script recorded package identity, signature, notarization, native integration, independent Calculator readback and microphone failure. An isolated installed-helper test exercised a real temporary Ledger save, correction and independent readback without touching owner records. The existing findings manager recorded all 29 per-issue outcomes and rendered the report; automatic prerequisite propagation remains future work.
- **Outcome:** ACU 0.2.107 installed from PR commit ea56232; 135 focused installed-package tests passed. The run recorded one fixed, one not fixed, 14 blocked and 13 inconclusive. The already-running client worked before refresh but rejected the new service afterward. The release remained not ready because no default microphone input was available; a spoken preference task and application remain unverified.

### Iteration review: 0.2.108

- **Delete:** Removed the false complete-match interpretation when the Ledger reports more results. The helper returns an explicit disambiguation state instead of asking its selector to decide from an incomplete set.
- **Simplify:** Reused the Ledger's existing `has_more` signal and existing telemetry pipeline; no second tracker or correlation store was added. Spoken model adherence remains unverified.
- **Optimize:** The frozen-head release source gate took 74.87 seconds, compared with 74.47 seconds for 0.2.107; these individual runs do not establish a speed change. The full gate ran once in this iteration.
- **Automate:** Added an isolated real-Ledger regression to the existing cross-plugin integration suite. A persistent MCP process tested client survival across refresh; it passed before and failed after the update. Reuse the release receipt and findings manager for the installed pass.
- **Outcome:** ACU 0.2.108 installed from PR commit 09ba6dc; 137 focused installed-package tests passed. All 29 issues received outcomes: one fixed, one not fixed, 14 blocked and 13 inconclusive. The release remained not ready because microphone preflight found no input. Automatic spoken preference use and active-client refresh remain open.

### Iteration review: 0.2.109

- **Delete:** Retained the runtime identity guard; a naive restart that drops buffered requests or an in-place identity override would be unsafe.
- **Simplify:** The candidate reuses the existing app and service contract and hands off only before dispatch, retaining the request, buffered input and client identity. Source tests cover mixed framing and reject a mismatched or incompatible target.
- **Optimize:** The frozen release source gate ran once. No comparable end-to-end time saving is established.
- **Automate:** The existing subprocess tests exercised actual process replacement with queued requests; the installed persistent-client probe recorded the pre-existing 0.2.108 client's failure. A later refresh must test a client containing the handoff code.
- **Outcome:** ACU 0.2.109 installed from PR commit 3cfbdd6; 137 focused installed-package tests passed. The run recorded one fixed, one not fixed, 14 blocked and 13 inconclusive. Microphone preflight failed. Source review identified a read-only identity-probe reconnect edge case, which must be corrected before testing handoff acceptance.

### Iteration review: 0.2.110

- **Delete:** Kept action retries disabled. Only the read-only service identity probe may reconnect after a closed socket.
- **Simplify:** Reused the existing `CUAClient` reconnect behavior for the identity probe instead of adding a second reconnect mechanism.
- **Optimize:** The frozen release source gate ran once; no comparable end-to-end improvement has been established.
- **Automate:** A persistent 0.2.109 client exercised the actual refresh: its first post-refresh call failed and its second successfully handed off. The source regression uses the real client retry path; a 0.2.110 client must still be tested across another installed refresh.
- **Outcome:** ACU 0.2.110 installed from PR commit a551690; 137 focused installed-package tests and 52 installed MCP tests passed. All 29 outcomes were recorded: one fixed, one not fixed, 14 blocked and 13 inconclusive. Microphone preflight failed; the first-call handoff and spoken acceptance remain unverified.

### Iteration review: 0.2.111

- **Delete:** No identity guard or action verification was removed. The previously added explicit input buffer prevents a process replacement from dropping queued messages.
- **Simplify:** Reused the installed persistent-client probe and existing release receipt to exercise the same client before and after the service update. Old processes that predate handoff still need reconnection.
- **Optimize:** The frozen release source gate ran once. No comparable end-to-end performance improvement has been demonstrated.
- **Automate:** The existing installed MCP and Ledger suites passed, and the persistent-client probe confirmed first-call survival and the new server version. The findings manager recorded the complete issue pass and rendered the HTML.
- **Outcome:** ACU 0.2.111 installed from PR commit ca1621b; 52 installed MCP tests and 137 focused installed voice/Ledger tests passed. One persistent 0.2.110 MCP client survived the refresh to 0.2.111 on its first state call; AGENT-06 is verified for clients containing the handoff. The run recorded two fixed, 12 blocked and 15 inconclusive. The release remained not ready because microphone preflight failed; spoken acceptance and island close/reflow remain unverified.

### Iteration review: 0.2.112

- **Delete:** No Ledger record or approval metadata was modified. Removed the mistaken implication that a pending application outcome means a user decision is awaiting approval.
- **Simplify:** Added a bounded explanation to existing Ledger results and instructions, preserving the original outcome and trust tier instead of creating another authority or changing stored decisions.
- **Optimize:** One installed text-only task provided task-linked lookup and independent exact-site readback without a voice session. One successful observation is not a measured latency improvement or general reliability proof.
- **Automate:** Existing source and installed suites checked outcome semantics; the findings manager recorded the installed issue pass. A persistent 0.2.111 MCP client survived the refresh on its first call.
- **Outcome:** ACU 0.2.112 installed from PR commit 2adb5b0; 139 focused installed voice/Ledger tests and 52 installed MCP tests passed. The signed helper returned the expected saved profile in one text-only task without being told to consult Ledger; independent readback matched an active site-scoped decision, distinct from the separate profile-selection-rule record. The run recorded two fixed, 11 blocked and 16 inconclusive. Microphone preflight failed, and spoken use, actual profile opening and island close/reflow remain unverified.

### Iteration review: 0.2.113

- **Delete:** Reject an empty after-observation as proof of a negative text postcondition; this prevents false completion but does not establish window closure.
- **Simplify:** The wake test waits on its event rather than a timer that could fire before the wake request. The first release failed that test; the next frozen-head source gate passed.
- **Optimize:** An agent-written prompt in an audio-enabled installed helper exercised the Ledger path without requiring the owner to speak. It failed with invalid arguments, so no performance or behavior success is claimed.
- **Automate:** Existing installed suites passed 53 MCP tests and 139 voice/Ledger tests; the persistent-client refresh probe passed on its first call. The menu-bar toggle was unavailable through the current app accessibility target, so a direct service-owned control is needed for repeatable menu-equivalent start and stop.
- **Outcome:** ACU 0.2.113 installed from PR commit 44e5824; signature, notarization, native integrations and microphone preflight passed, but release voice smoke timed out without a spoken interaction. The issue pass recorded two fixed, two not fixed, seven blocked and 19 inconclusive. User accepted the idle pill size on the prior build. Helper-only island captures do not prove menu-controlled reflow.

### Iteration review: 0.2.114

- **Delete:** The release can skip the unattended spoken smoke without falsely declaring readiness; it retains an explicit pending result rather than asking the user to speak during an unattended run.
- **Simplify:** `voice_control` uses the existing menu supervisor and local service contract for status, start and stop, instead of adding an independent voice lifecycle. It does not retry a start or stop automatically.
- **Optimize:** An audio-enabled session accepted agent-written prompts. One Calculator computation was independently read back; an attempted close failed and remains open. No comparative speed saving is claimed.
- **Automate:** Installed control status, stop and start passed with independent gateway readiness readback. The agent captured and compared on/off states with a Codex activity, then restored Samantha on and cleared its own activity. Installed suites passed 55 MCP and 139 focused voice/Ledger tests.
- **Outcome:** ACU 0.2.114 installed from PR commit 67173c9. The run recorded three fixed, three not fixed, three blocked and 22 inconclusive. Codex visibly reflowed beside the island after supervisor stop; user-approved idle width remains fixed. Sampled still frames showed no obvious clipping but do not establish motion quality. Calculator close and automatic Ledger preference lookup remain failing; spoken release acceptance remains pending.

#### Post-pass checks on the same installed build

- An agent-written Netflix request failed with `ledger_invalid_argument`; a separate read-only scoped Ledger lookup confirmed matching saved decisions, and Comet had no open window. The current validator rejects a noncanonical scope before Ledger access.
- An agent-written Calculator task returned 221 with independent AX readback. The close request failed and AX still showed the window open. Developer Observability linked both task runs and the Ledger error, but continued to label task outcomes unverified.
- A controlled supervisor stop/start took about 1.1 seconds per call, restored an audio-ready 0.2.114 session, and showed the idle pill reflow in still captures. Screenshots did not establish animation quality.
- **Delete:** Avoided a second 31-item run on the unchanged install; appended follow-up evidence to the existing completed run instead.
- **Simplify:** Reused the installed `/api/session/text` route and independent AX readback for controlled checks, then the findings manager rendered the report in the same batch.
- **Optimize:** Measured 8.19 s and 15.86 s for the two typed requests; without a comparable baseline, no speed improvement is claimed.
- **Automate:** The remaining root-cause check is deterministic: normalize a bare site/app scope to `tool:<slug>` and add a regression test before the next release iteration.

### Iteration review: 0.2.115

- **Build and run:** ACU 0.2.115 build 668 was installed from PR revision `2b127aea50c85b0d28eb179b63810a9eeb4f44a5`. App and Codex plugin manifests both read 0.2.115. The separate release microphone preflight reported `no_input_device` as expected after the user's disconnect.
- **Outcome:** The fresh issue-navigation run completed all 31 checks: 12 fixed (including preserved fixes), 10 not fixed, 4 blocked, and 5 inconclusive. The Netflix task did not apply the saved preference; Calculator closed but Samantha reported the wrong prior state; task outcome/build attribution remain incomplete. Live layout telemetry still puts the voice slot 6–30 pt outside the 74 pt island even though 16 source layout tests pass. Microphone-only checks remain blocked.
- **Independent checks:** The typed Netflix run linked Ledger, Comet and outcome events, but its Ledger result was ambiguous and the browser stayed at profile selection. Three supervisor cycles started healthy in 1.099–1.114 s. Current source tests passed for 3 log-rotation cases, 1 closest-slot reflow case, and 16 voice/app bounds cases. Live log-rotation concurrency and reflow to an identifiable surviving Codex mark were not established.
- **Delete:** Do not repeat menu-bar captures for every issue. One controlled on/off/on capture set covers the visual transition; targeted observations can then reuse that evidence.
- **Simplify:** Use the status returned by each supervisor start/stop call as its immediate readback; make one separate status call only when a later operation needs an independent baseline.
- **Optimize:** Batch all issue outcomes through the findings manager and render once per batch. Use one fresh observability collection after the controlled task group, then inspect only linked runs and implicated issue IDs.
- **Automate:** Keep using the existing manager for run creation, batched outcomes and HTML generation. The next fix cycle should add a deterministic runtime geometry assertion that compares the emitted voice-slot rect to the actual pill bounds, since source tests and installed telemetry disagree.

### Candidate: ACU 0.2.116 and Developer Observability 0.1.18

- The candidate clarifies agreement between complete relevant Ledger decisions, records bounded scope categories, and distinguishes an exited app during a planned action. These changes are not yet installed or verified as fixes.
- The Netflix task returned unrelated Ledger decisions; a separate scoped helper lookup returned the saved decisions. The task did not record its scope, so the cause remains unconfirmed.
- The combined `scripts/check.py --fast --jobs 2` source gate passed 118 test processes. Installed integration tests were skipped and remain required after installation.

### Iteration review: 0.2.116

- Installed PR commit `3313bd166a6d8bfe1e1e831909d1b2981604c5ae`. Signature, notarization, fresh MCP, client, cursor and live-fd checks passed. Microphone preflight failed because the input was disconnected.
- The 32-issue run recorded 9 fixed, 6 not fixed, 5 blocked and 12 inconclusive. A typed Netflix task queried fleet scope twice and left the profile chooser open. A typed Calculator task produced 221 and closed the process, but its timeline recorded an action error and a subsequent state error. Two current resize records report voice-slot bounds violations.
- **Delete:** Avoid a second scoped Ledger read to infer the task input when task-linked scope categories are available.
- **Simplify:** Reuse one on/off/on screenshot set for control, reflow and icon observations; keep animation and icon identity inconclusive when stills cannot establish them.
- **Optimize:** Collect observability once after the task batch and filter by run ID. This is a process change, not a measured speed claim.
- **Automate:** The new bounded scope category made the wrong fleet scope visible in the task timeline. It cannot distinguish individual non-browser tools, and complete save, audio and motion checks remain outstanding.

### Candidate: ACU 0.2.117

- Clarify the named site/app scope rule and return an explicit `app_not_running` state for the observed missing-process response. The site selection remains model-controlled and needs installed verification.
- The 88 focused Ledger and compact MCP tests and the frozen 118-process source gate passed. Installed integration and live verification remain pending.

### Iteration review: 0.2.117

- Installed PR commit `1b31ec1c42bdca3228f997f4dc446b7fcbf94fab`. Source, signature, notarization, fresh MCP, client, cursor and live-fd checks passed. Microphone preflight initially failed; after connection a new session reported audio ready.
- The typed Netflix retry retrieved a matching site decision but asked which profile to use. Calculator displayed 221 and closed, but its linked action reported `observation_incomplete`. Current resize telemetry still reports voice-slot overflow.
- **Delete:** Do not repeat a read-only Ledger lookup when the same task's linked result already shows what was retrieved.
- **Simplify:** One on/off/on capture set covers control, reflow and icon presence; it does not prove animation or the original icon's identity.
- **Optimize:** Page one existing long session to validate timeline coverage instead of generating hundreds of new events. No comparable speed baseline was measured.
- **Automate:** The existing task-linked scope diagnostic exposed the incorrect fleet lookup in 0.2.116 and confirmed a tool-scope lookup in 0.2.117. Automatic decision application remains failing.

### Candidate: ACU 0.2.118

- Clarify use of a relevant saved self-reported preference without claiming formal attestation. Preserve an observed app exit in action feedback without declaring unverified dispatch successful.
- The 89 focused tests and frozen 118-process source gate passed. Installed behavior and the remaining issue pass are pending.

### Iteration review: 0.2.118

- Installed PR commit `b7b3113984fc098bb0382e5074098a98b92a425c`. Source, signature, notarization, microphone, fresh MCP, client, cursor and live-fd checks passed. Spoken acceptance remains pending.
- The corrected 32-issue pass recorded 10 fixed, 6 not fixed, 2 blocked and 14 inconclusive. The earlier chooser readback used another Comet window and is superseded; read-only `page_context` from Samantha's exact lease showed the Netflix home page personalized for the expected profile. Calculator displayed 221 and closed while its action reported `observation_incomplete`. Current telemetry recorded two voice-slot bounds violations.
- Samantha must infer when to recall relevant decisions and when to save durable user preferences without being asked to use Ledger. The installed typed recall passed; independent automatic-save and spoken checks remain open.
- **Delete:** Avoid repeating a readback already tied to the exact task and browser lease.
- **Simplify:** One on/off/on capture set covers multiple island issues, with a second stopped capture showing that the waveform disappeared after settling.
- **Optimize:** Reuse a single post-batch observability collection and filter by task run. No speed improvement is claimed without a comparable baseline.
- **Automate:** Bind browser postcondition readback to the task's lease and window identity to prevent wrong-window conclusions. An isolated automatic-save test remains needed.

### Candidate: ACU 0.2.119

- A controlled installed 0.2.118 Calculator quit reproduced `observation_incomplete`; a trace showed screenshot fallback raising `app_not_found` after the key action, discarding the action result. Preserve the dispatch result and the fresh observed app absence when this race occurs; other screenshot errors still propagate.
- A focused regression passes. A worktree MCP live check cannot use the installed service because the runtime contract hands off to the installed package; candidate acceptance requires the exact pushed and installed build.

### Iteration review: 0.2.119

- Installed PR commit `db62c228105af7d086bc68fde6d79e1d3cfda338`; source, signing, notarization, fresh-client, cursor, file-descriptor and microphone checks passed. Spoken acceptance remains pending.
- The installed pass covers all 32 findings: 8 fixed, 7 not fixed, 1 blocked and 16 inconclusive. The fresh standalone MCP retained the dispatched close result and observed app exit, but Samantha's linked close task still logged `observation_incomplete`; independent process readback confirmed Calculator exited. The broader native-action issue remains failing.
- A fresh typed Netflix request made a narrow site-scoped lookup that returned no matches and asked which profile to use. The 0.2.118 success remains historical evidence, but the new failure establishes inconsistent automatic preference recall. Automatic save/update remains blocked by the lack of an isolated writable Ledger fixture.
- The user subsequently reported that spoken recall also failed, including when they explicitly asked Samantha to check Ledger. A recent voice session contains a rule-filtered lookup returning no decisions; speech events do not carry a run reference, so the match is time-correlated rather than proven. Source inspection shows `lookup` returns `none` immediately on an empty rule-filtered result, while `recall` separately searches candidates without the rule. This is a next-PR root-cause lead; the correct fallback must preserve scope and conflict/pagination safeguards.
- The user reports Samantha stopped in the requested spoken cancellation test. The recent voice session has no linked, timestamped cancellation utterance, so the ordering of cancellation and subsequent actions remains inconclusive.
- Installed-package rotation tests passed for Python and Swift, including concurrent writers to a temporary log; running-service stress and crash recovery remain unproven. Current 0.2.119 resize telemetry reports a 30 pt voice-slot overflow; physical clipping was not independently established.
- **Delete:** Avoid rerunning the full source gate separately immediately before a release that already runs the same gate; this iteration ran both.
- **Simplify:** One task-linked observability timeline and independent app readback cover calculation, close, attribution and diagnostics, while preserving the distinction between standalone MCP and Samantha.
- **Optimize:** Freeze the candidate once and use the release gate's result as the source-gate receipt. No speedup is claimed without comparable measurements.
- **Automate:** Add an isolated writable Ledger fixture and task-to-browser-lease identity checks in the next PR; neither is established by the existing tests.
- At the user's request, PR #87 displays the 13 previously verified fixes. The same JSON retains the 19 unresolved findings and their evidence under `tracking.deferred_findings` for the follow-up PR; current-install inconclusive checks do not erase earlier verification.

### Follow-up PR

- The existing tracker now displays the 19 unresolved findings; the 13 previously verified findings are retained under `tracking.archived_findings` and remain visible on PR #87.
- Candidate ACU 0.2.120 retries a rule-filtered empty lookup without the rule while keeping the exact scope, then applies the existing selection and incomplete-result checks. Focused source tests pass; installed and voice acceptance are pending.
