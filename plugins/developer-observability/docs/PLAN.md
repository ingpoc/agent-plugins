# MCP interface plan

Data owner: a findings producer's prepared report. The read-side backend is
canonical here; collection, recommendation
evidence preparation and Decider validation stay with their owners.

| Tool | Contract |
| --- | --- |
| `observability_projects` | Configured IDs, attribution basis, source coverage, freshness and unassigned count |
| `observability_issues` | Scoped bounded issue list; stable IDs and snapshot-bound pagination |
| `observability_tasks` | Recent task summaries indexed by run_ref, with no session lookup required |
| `observability_task` | One run_ref-linked task with ordered steps and first failure |
| `observability_task_detail` | One capped task-step facet with snapshot-bound pagination |
| `observability_inspect_issue` | Observed failure, rule-based lead, bounded redacted evidence and outcome provenance |
| `observability_storage` | Disk use of observability paths and the 0.1.8 prune plan (dry run by default) |
| `observability_recommendation` | Read existing Ledger pilot; explicit propose reuses owner locking/validation |
| `observability_decide` | One bounded typed choice; fresh snapshot, unchanged confidence/uncertainty, no action |
| `observability_dashboard` | Same projections in one negotiated MCP Apps view; structured fallback |

## Boundaries

Configuration explicitly binds local owners and label mappings. Unique explicit
owners take precedence. Ambiguous or unknown findings remain unassigned. Scope
IDs are not multi-user authorization. No arbitrary file/log readers, raw
transcripts, credential tools, collector restart or repair execution.

Freshness and coverage are visible on every data view. Cursors bind revision,
project and query. Investigation closure never becomes independent acceptance.
Generic projects receive recorded rule-based leads; model recommendation coverage
remains the bounded Ledger pilot. No new recommendation engine or acceptance
store is introduced.

## App

One dashboard with project/status selectors, coverage strip, observations,
detail and expandable evidence; recommendation/actionability panel and explicit
typed-decision form. Render evidence as text. Use the official MCP Apps SDK,
self-contained HTML, constrained resource policy and host bridge. Reset scoped
state and discard superseded requests. No background model calls or repair button.

## Acceptance

1. Boundary tests: scope, ambiguous attribution, cursor revision, caps, privacy,
   quoted values, multiline context, failure without fallback, retained proposals.
2. Actual stdio discovery/tools/resources against current and legacy clients;
   syntax-check shipped JavaScript and reproducible build.
3. Installed launcher reads actual prepared data; explicit local decision;
   owner proposal isolated from production; verify state and side effects.
4. Visible Comet acceptance host: initialization, project switching, detail,
   evidence pages, recommendation provenance and layout. Native host proof stays
   separate from this bridge-host proof.
5. Paired known-issue retrieval: same checked fields, response token equivalents
   and warm wall time; report startup and scope limits separately.
