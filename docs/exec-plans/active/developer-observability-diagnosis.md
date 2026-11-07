# Developer Observability diagnosis

## Goal and usage

An agent needs a compact, project-scoped view of the strongest observed leads,
their evidence and the next investigation step. `observability_diagnose()`
discovers projects; `observability_diagnose(project_id=...)` returns a bounded
triage view. Passing selected issue IDs and that view's snapshot ID expands
bounded evidence. A changed snapshot requires a new triage call.

## Shape and ownership

The existing collector owns source events and attribution. The read-side
backend owns snapshot scoping, redaction, ranking and evidence projection; the
MCP server adapts that interface. Existing issue and session tools remain for
pagination and detailed timelines. Diagnosis never upgrades a recorded lead to
a proven cause, infers a task join from timing, or treats a reported outcome as
independent acceptance. Missing provenance and stale or partial coverage remain
visible. Detailed diagnostics stay opt-in.

```python
def diagnose(project_id: str | None = None, issue_ids: list[str] | None = None,
             snapshot_id: str | None = None, top: int = 5,
             include_diagnostics: bool = False) -> dict: ...
```

## Synthesis decision

Use one diagnosis entry point over the existing snapshot, rather than separate
new brief and batch tools. It hides selection and snapshot consistency while
retaining existing detailed APIs. The alternative task-evidence model identifies
the necessary longer-term producer boundary: explicit task references, build
attribution and independent verifier receipts must originate from their owners.
Adding a task projection before producers emit those links would create a second
interface with no reliable new evidence. A single snapshot also cannot prove a
new/resolved delta.

An independent cross-judge preferred this base for present-day usefulness and
truthful attribution. It identified a need to label overview examples without
claiming cross-kind priority and to give diagnosis-specific snapshot recovery.
The task-evidence candidate supplies the future producer contract: exact opaque
task joins and independent verifier receipts, never timestamp-based joins.

## Acceptance and limits

Test scoped and empty results, bounded redacted expansion, invalid IDs, and
snapshot changes through the MCP transport. Verify the installed client on a
controlled task before claiming the new tool works there. Compare call counts
on the same task; do not infer efficiency from interface shape. Task identity,
cross-tool timing, Ledger decisions and independently verified outcomes require
producer changes and separate live acceptance.
