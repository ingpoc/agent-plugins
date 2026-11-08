# Version-scoped observability

## Usage

An agent selects a project, discovers observed version/attribution scopes, then
requests diagnosis and detail for one scope. The scope is explicit on every
request; changing it invalidates cursors and detail references. `event` means
the producer recorded the version; `session_inferred` and `unknown` are separate
scopes. An observed version is not proof of the deployed build.

```python
diagnose(project_id, version=None, attribution=None, issue_ids=None,
         snapshot_id=None, top=5, include_diagnostics=False)
issues(project_id, status='all', cursor=None, limit=20, kind='all',
       version=None, attribution=None)
sessions(project_id, session_ref=None, ..., version=None, attribution=None)
```

## Shape

The collector partitions normalized events by project, version and attribution
before any rollup or metric calculation. It stores bounded, redacted scoped
projections inside the same atomically replaced snapshot. The read API selects
one projection and binds pagination and issue/session identities to its scope.
Legacy primary findings without scoped provenance are excluded and disclosed;
they cannot establish that a selected version has no issues. Unscoped reads
remain explicitly historical aggregate views for compatibility. No normalized
event archive or invented deployment inventory is introduced.

## Synthesis

Two candidates compared collector-side scoped projections against a retained
event archive with read-time aggregation. The independent judge preferred the
collector-side projection because it preserves the existing ownership and avoids
retaining extra potentially sensitive events. The rejected alternative provides
more flexible queries but increases retention, query cost and privacy exposure.
Both require attribution-aware partitioning, including mixed-version sessions,
and do not prove the deployed version.

## Acceptance

Use controlled mixed-version events with distinct failure counts, latency,
redundancy, layout and session evidence; verify exact, inferred and unknown
scopes separately, including old/primary snapshots and cross-scope cursors.
Exercise the installed MCP and dashboard after release. A successful source
test is not proof of deployment attribution or live acceptance.
