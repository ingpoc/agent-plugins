# Trail and job-usage emitter contract

Readers: emitters of `~/.agents/state/batched-change-list/**/events.jsonl` and the change-list tracker
(today: ACU's BCL scripts). developer-observability only reads these fields; it never writes them.

Every field below is optional. When a field is absent the API says so (`provenance: "missing"`,
`tokens_source: "none"`, `model: null`) or falls back to a value it labels `derived`. It never guesses.
Times are ISO-8601 with an offset (`Z` or `+05:30`).

## 1. Job events (existing `start` / `finish` rows, same `job_id`)

| Field | Type | On | Meaning |
|---|---|---|---|
| `finding_ids` | string[] | start and finish | Issue ids this job works on. Without it a job is credited only through its lane (`group_id`), shown as derived. |
| `group_id` | string | start | Lane id (`R2-DO4`). |
| `round` | int | start | Round number. |
| `model` | string | start or finish | Provider model name, e.g. `gpt-5.1-codex`. Not an agent or script name (`Grok Bot`, `run-x.sh` are rejected). |
| `model_version` | string | start or finish | Version or snapshot, e.g. `2026-11-01`. |
| `tokens` | object | finish | `{"input": int, "output": int, "total": int}`. `total` is required for any count to show. |
| `tokens_source` | `"stamp"` \| `"official"` \| `"estimated"` \| `"none"` | finish | `stamp`/`official` = the provider's usage; `estimated` = counted by the emitter. |
| `tokens_method` | `"harness_usage"` \| `"char_estimate"` | finish | How the count was obtained. |

Example finish row:
```json
{"event":"finish","job_id":"8f2c…","at":"2026-11-24T16:10:02Z","finished_at":"2026-11-24T16:10:02Z","exit_code":0,
 "phase":"fix","group_id":"R1-TOKENS","round":1,"finding_ids":["DO-6","BCL-147"],
 "model":"gpt-5.1-codex","model_version":"2026-11-01",
 "tokens":{"input":677339,"output":4361,"total":681700},"tokens_source":"stamp","tokens_method":"harness_usage"}
```

## 2. Trail events (new rows in the same `events.jsonl`)

`event` is one of `issue_failed_first`, `root_cause_identified`, `fix_verified`.
Do **not** set `job_id` on a trail row (the job folder merges rows by `job_id`); use `by_job_id`.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `event` | string | yes | One of the three names above. |
| `finding_id` or `finding_ids` | string or string[] | yes | Issue id(s). |
| `at` | ISO-8601 | yes | When the step happened. |
| `run_id` | string | recommended | Verification run id (`r3-2d1e049c-121`) or lane id for a root cause. |
| `evidence` | string | recommended | Pointer: `check:<run>:<id>`, `job://<job_id>`, a file path or URL. |
| `summary` | string | optional | At most 6 words for people, e.g. `stale cache key`. |
| `by_job_id` | string | optional | Job that produced the step. |

Examples:
```json
{"event":"issue_failed_first","finding_id":"DO-6","at":"2026-11-24T10:11:18Z","run_id":"r1-9a1b-122","evidence":"check:r1-9a1b-122:DO-6","summary":"0 of 8 jobs had tokens"}
{"event":"root_cause_identified","finding_id":"DO-6","at":"2026-11-24T10:40:00Z","run_id":"R1-TOKENS","evidence":"job://8f2c…","summary":"Runner drops the usage block","by_job_id":"8f2c…"}
{"event":"fix_verified","finding_id":"DO-6","at":"2026-11-24T12:30:00Z","run_id":"r2-77aa-122","evidence":"check:r2-77aa-122:DO-6","summary":"8 of 8 jobs stamped"}
```

The tracker can instead store the root cause on the finding:
`findings[].root_cause = {"at": ISO-8601, "run_id": string, "evidence": string, "summary": string}`.
Both forms are read as `recorded`.

## 3. How the API uses them

`GET /api/issues?...&include=trail,jobs,rollup`, `GET /api/issues/<id>` and the `observability_issues`
tool (`include: ["trail","jobs","rollup"]`, `issue_id`) return, per issue: `trail[]`, `cycle`, `timing[]`,
`after[]`, `jobs_usage[]`, `tokens`; and at the top level `rollup[]` and `include`. Fallbacks today:

| Step | Recorded from | Derived fallback |
|---|---|---|
| first_fail | trail event, `failure_history`/review rows, failing check, structured `first_seen` | `observed` prose, `created_at` |
| root_cause | trail event, `findings[].root_cause`, a `plan_review` job with `finding_ids` | lane plan time: plan `recorded_at`, else the first job on the lane, else the round start |
| fix_verified | trail event, passing `verification_history` check, `state_changes` to resolved | `verified_at`/`resolved_at`, else the PR merge time |
| regressed | `state_changes` to regressed / owner rejection, failure after a pass | — (ts missing) |

A duration is `recorded` only when both ends are recorded. Official and estimated tokens are never summed.

## Cursor CLI jobs
For a Cursor CLI job, emitters don't need to count anything. Read `bin/developer-observability cursor-usage --run <uuid>` (or `--session`) and copy `model`, `tokens`, `tokens_source` and `tokens_method` as they are. `model_version` and cost stay null because Cursor does not report them. See `skills/developer-observability/references/cursor-cli-runs.md`.
