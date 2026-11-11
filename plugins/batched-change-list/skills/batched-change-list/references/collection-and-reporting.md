# Collection and reporting details

## Collect and test

1. Identify the product, installed build or revision, test scope, reference or expected behavior, and what the user has authorized. Preserve existing work. Test the real artifact using the appropriate project and platform tools.
2. Record each finding once with an ID, affected state, expected and actual behavior, severity, status, and concrete evidence. Attach screenshots, logs, reproduction steps, or links when available. Mark missing evidence, uncertainty, and untested paths explicitly; observability events are supporting evidence, not proof of cause. Keep unrelated work separate.
3. Add feedback as it arrives without starting a fix for every item. If the user is actively testing, give a short ready-to-run test card with the exact prompt or actions, expected result, and what to report back. Record the app/build and a timestamp or run/session reference when available; attribute telemetry to that test only when evidence links them, otherwise label it as an unlinked lead. Fix urgent security, data-loss, or blocking issues promptly; do not delay explicitly requested work for batching.

Collect what the agent can verify itself before asking the user to test. Refresh the relevant observability snapshot, include agent-observed failures and group related telemetry leads with stable IDs and provenance. Link evidence to an existing issue where it describes the same problem; do not count records as separate bugs. Keep unlinked leads explicitly unconfirmed. Before a build or install, identify the required PR, release gate, installed path, and acceptance checks so one authorized cycle covers the batch.

## Present the list

Render a standalone, self-contained page from the existing issue owner's findings, using [`examples/findings.json`](../examples/findings.json) as the export shape. Run this from the skill directory:

```sh
python3 scripts/render.py /absolute/path/findings.json /absolute/path/change-list.html [--inventory /absolute/path/inventory.json]
```

The generator validates IDs, sources and canonical statuses, requires a verification summary and references for `verified_fixed`, safely embeds the data and local cropped PNG/JPEG/WebP/GIF evidence, and derives navigation, selected panel, badges and counts from the same records. `evidence` entries use `src` (path relative to the JSON), `alt`, `caption`, and optional `full` (HTTP(S) URL or path relative to the HTML). Missing proof is an error, not proof of acceptance; the agent must judge whether references actually establish it. Export from the existing owner; the JSON is not a second tracker. Never paste personal data or secrets into a public report. Show one selected issue at a time with a short problem, the reason for its latest check outcome, and a relevant crop. Label an older result as previous when the current install is unchecked. Put provenance, older evidence and scope in `details`. Verify navigation and selected panel at desktop and mobile widths, then link the page.

Keep current status, build and acceptance in the current check and summary. Label older observations by build and date, never as the current install. Consolidate repeated explanations; retain distinct evidence and verification history. Review stale labels, repeated details and obsolete user-test instructions before rendering. Do not automatically delete evidence.

Serve the current report at `http://127.0.0.1:53366/` from the skill directory using `python3 scripts/serve.py /absolute/path/change-list.html`. For live rendering from the authoritative JSON, add `--data /absolute/path/findings.json`; add `--inventory /absolute/path/inventory.json` to show the supplied installed-plugin inventory. If that port already serves the exact generated report, reuse it; compare its response bytes with the file. If another report or service occupies the port, report the conflict and preserve it; do not stop another owner or silently switch ports. For a new PR, update the served report only after verifying ownership. Keep the viewer running during the active run. The page checks for changes automatically; local-file views require manual refresh.

The HTML page is the user's only view of progress. Use `set-activity` in `batch --render` as each phase or issue-group step starts; it updates the stage, affected issue statuses, and one timestamped `Now` line. Stored plugin inventory is labeled `As recorded`; pass a current inventory file at render time when one is available.

At rollover, set `tracking.rollover.excluded_ids` to the IDs already verified fixed in the merged PR. Keep their records and history in the tracker; rendering excludes those IDs while retaining carried-over, newly added, and subsequently fixed findings. Render the new PR-named report and switch the owned viewer to it before removing the old worktree; verify the served bytes match the new report.

Use one `batch --render` operation for the rollover: `{"action":"rollover","source_pr":N,"pull_request":{"label":"PR #M","url":"…"},"worktree":"new-name","now":"…"}`. It sets `excluded_ids` to every `verified_fixed` finding (keeping earlier exclusions), writes `unresolved_ids`, updates the page header's PR and worktree, and sets the stage to `report_rollover`. Do not hand-edit those fields.

Merge sequence when the verified-fix threshold is met: mark the draft PR ready, wait for the required CI check on the exact head (fix a flaky test at its root and push if it fails), merge, retarget any stacked PR to the base, open the roll-over PR from the new base, run `rollover`, rename the worktree folder to match, and restart the viewer on the new path so the page serves the new header.
