# Tracker operations

The JSON is authoritative; render it after each mutation. If only a standalone HTML page remains, the manager can read or patch its embedded array; pass `--collection` for a legacy array name. For a committed report, provide `tracking.pull_request.label`, `tracking.pull_request.url`, and `tracking.worktree` in the JSON.

`get` omits large inline screenshot payloads by default; pass `--include-evidence` only when you need the image bytes. `list` stays compact.

Set `SKILL_DIR` to the absolute `batched-change-list` skill directory before running these commands.

```sh
python3 "$SKILL_DIR/scripts/manage_findings.py" audit /absolute/path/findings.json
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

`record-evidence` appends to the authoritative JSON only. Supply six nonempty string fields: `check`, `outcome`, `build`, `session`, `timestamp`, and `reference`; use `unknown` when a field is unavailable. Timestamps use ISO 8601 with a timezone. The report shows the latest dated record or completed run check for each primary build version; undated records cannot supersede dated results. Records never change status automatically. Render after updating the JSON.

For several changes, `batch` accepts an array of `add` (`entry`), `update` (`id`, `patch`), `delete` (`id`), or `record-evidence` (`id`, `record`) operations. Evidence timestamps must be ISO 8601 with a timezone or `unknown`. The JSON is validated and written once; `--render` renders once. If the HTML write fails after the JSON write, rerun the renderer from the authoritative JSON.

`audit` reports possible stale version claims and exact repeated details for human or agent review. Warnings are heuristics, not proof, and never modify or delete data.

For the workflow timeline, use `batch --render` with `[{"action":"set-stage","stage":"collect"}]`. Allowed stages are `collect`, `present`, `plan_fix`, `install`, `verify`, and `report_rollover`; use `null` when stopping or pausing. Set the stage before beginning each phase, including when returning to an earlier phase. This updates the authoritative JSON and page together. The timestamp is the last reported stage, not a liveness check.

If stage update writes JSON but rendering fails, regenerate the same HTML from the authoritative JSON before relying on the timeline. After interruption, check the timestamp and explicitly set the resumed phase; never treat the old highlight as proof of an active agent.
