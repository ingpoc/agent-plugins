# Tracker operations

Adopt an in-repo tracker once with `bcl --data FINDINGS.json adopt`. The live JSON is authoritative under `$BCL_STATE_DIR/<repo>/<list-id>.json` (`~/.agents/state/batched-change-list` by default); the registry records the home worktree and JSON/HTML snapshot paths. Tests must set `BCL_STATE_DIR` to a temporary directory. The HTML beside the in-repo snapshot is for review; every successful stage snapshots both files and commits them only in the home worktree. Provide `tracking.pull_request.label`, `tracking.pull_request.url`, and `tracking.worktree` where applicable.

Use `plugins/batched-change-list/bin/bcl --data FINDINGS.json <stage>` for workflow stages. Every call resolves the live file and starts or restarts its per-list viewer; the viewer port and process are recorded under `$BCL_STATE_DIR/viewers`. `plan --plan PLAN.json` replaces the plan for the current cycle; `update-plan-group` accepts `label` and `verify_summary` along with status, codex and verify updates. Install requires a nonempty current-cycle plan, and Verify requires Install to have been reached. `bcl merge` refuses unsnapshotted live changes and never merges branches. `bcl install-hooks` adds repository pre-commit and pre-push report freshness checks.

`get` omits large inline screenshot payloads by default; pass `--include-evidence` only when you need the image bytes. `list` stays compact.

Set `SKILL_DIR` to the absolute `batched-change-list` skill directory before running these commands.

```sh
python3 "$SKILL_DIR/scripts/manage_findings.py" audit /absolute/path/findings.json
python3 "$SKILL_DIR/scripts/manage_findings.py" compact /absolute/path/findings.json --render /absolute/path/change-list.html
python3 "$SKILL_DIR/scripts/manage_findings.py" list /absolute/path/findings.json
python3 "$SKILL_DIR/scripts/manage_findings.py" get /absolute/path/findings.json ISSUE-02
python3 "$SKILL_DIR/scripts/manage_findings.py" add /absolute/path/findings.json /absolute/path/new-finding.json
python3 "$SKILL_DIR/scripts/manage_findings.py" update /absolute/path/findings.json ISSUE-02 /absolute/path/patch.json
python3 "$SKILL_DIR/scripts/manage_findings.py" record-evidence /absolute/path/findings.json ISSUE-02 /absolute/path/evidence.json
python3 "$SKILL_DIR/scripts/manage_findings.py" batch /absolute/path/findings.json /absolute/path/operations.json --render /absolute/path/change-list.html
python3 "$SKILL_DIR/scripts/manage_findings.py" delete /absolute/path/findings.json ISSUE-02
python3 "$SKILL_DIR/scripts/manage_findings.py" get /absolute/path/change-list.html ISSUE-02
python3 "$SKILL_DIR/scripts/manage_findings.py" get /absolute/path/legacy-list.html ISSUE-02 --collection primary
```

`update` applies a JSON Merge Patch: nested objects merge, arrays replace, `null` removes a field, and `id` cannot change. JSON mutations validate the whole findings file before an atomic write; deleting the last finding is supported. HTML mutations replace only the selected embedded array, so use the JSON source whenever it exists and keep any separately authored summaries in sync.

When adding a finding, write `summary`, `impact`, and `done_when` in plain words for a non-technical reader. Put commands, SHAs, counts, timestamps, and log quotes in `observed` or `details`. Add/update and batch warn on log-like summaries; add also warns when `summary` is missing.

`record-evidence` appends to the authoritative JSON only. Supply six nonempty string fields: `check`, `outcome`, `build`, `session`, `timestamp`, and `reference`; use `unknown` when a field is unavailable. Timestamps use ISO 8601 with a timezone. The manager retains the latest passing, failing, and blocked/inconclusive evidence, any record cited by `verification_refs`, and the only record for an outcome, dropping older unreferenced records on append. Records never change status automatically.

For several changes, `batch` accepts an array of `add` (`entry`), `update` (`id`, `patch`), `delete` (`id`), or `record-evidence` (`id`, `record`) operations. Evidence timestamps must be ISO 8601 with a timezone or `unknown`. The JSON is validated and written once; `--render` renders once. Pass `--inventory /path/inventory.json` to `batch`, `compact`, or `scripts/render.py` to supply an inventory override. `scripts/render.py --live` discovers installed copies and uses the current time; `--now <ISO-8601>` injects a deterministic clock. With live discovery, stored versions are labeled `As recorded` only for plugin names no install was found for; the no-flag renderer remains deterministic. If the HTML write fails after the JSON write, rerun the renderer from the authoritative JSON.

`audit` reports possible stale version claims and exact repeated details for human or agent review. It exits nonzero for denied detail keys, values over 600 characters, more than 8 detail keys, over-retained verification evidence, carry-over checks, archived findings, or tracker JSON over 320 KiB; those violations direct the agent to `compact`. Warnings are heuristics, not proof, and never modify or delete data. Compact is deterministic and idempotent, prints byte counts before and after, removes `tracking.archived_findings`, drops matching process/history detail keys, trims long or excess details, and preserves all finding IDs and statuses. Extend the source-level `DETAIL_DENY_PATTERNS` list when more process or history key patterns need to be denied.

For the workflow timeline, use `batch --render` with `[{"action":"set-activity","stage":"plan_fix","ids":["ISSUE-01"],"now":"Inspecting the shared root cause"}]` as each phase or group step starts. This sets `tracking.workflow.stage`, marks the listed findings `in_progress`, and writes one timestamped `tracking.workflow.now` line below the stage bar. For a closed finding such as `verified_fixed`, pass `"reopen":true` explicitly; otherwise the operation rejects the closed IDs without changing the tracker. The page is the user's only progress view. Use `set-stage` with `null` when stopping or pausing. The timestamp reports when activity was recorded; it is not independent proof of work.

If stage update writes JSON but rendering fails, regenerate the same HTML from the authoritative JSON before relying on the timeline. After interruption, check the timestamp and explicitly set the resumed phase; never treat the old highlight as proof of an active agent.
