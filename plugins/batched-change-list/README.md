# batched-change-list

Collect related product findings once in a JSON tracker, render a reviewable HTML change list, and run fix, install and verification as batched cycles. Use when a test pass turns up several issues that must be tracked, shown to someone, and closed with evidence.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: [`AGENTS.md`](AGENTS.md). Collection routing: repo-root `AGENTS.md`.

## Overview

**Purpose.** Keeps one authoritative tracker for a batch of findings and one rendered view of it. The tracker records each issue once with status, evidence and verification history; the renderer produces a standalone HTML page with navigation, badges and embedded crops. The workflow phases (collect, present, plan/fix, install, verify, report/roll over) are written into the skill so a long QA cycle does not lose items, proof, or the user's priorities.

**Requirements.** `python3` only, standard library. No MCP server, no runtime install, no data directory, no credentials. The optional live viewer listens on `127.0.0.1:53366` and can be left off.

**Install.**

1. Install the `batched-change-list` plugin from this repository's marketplace.
2. Confirm the client lists the `batched-change-list` skill.
3. Point the tracker at the product under test, for example `<project>/docs/change-list/findings.json`, using [`examples/findings.json`](skills/batched-change-list/examples/findings.json) as the shape.

**Configuration.** None. Commands take absolute paths to the findings JSON and the HTML output. The viewer port is fixed so that one owner can reuse an existing page; a conflict is reported, never overridden.

**Network endpoints, permissions and credentials.** No outbound calls. The viewer binds loopback only and serves the generated page plus the evidence images embedded in it. Local evidence images are read from paths relative to the findings JSON.

**Testing.** From `skills/batched-change-list`: `python3 -m unittest discover -s scripts -p 'test_*.py'` (53 tests: tracker mutations, renderer validation, viewer).

**License.** MIT (repository root [`LICENSE`](../../LICENSE)).

## Benchmarks

This plugin has no 0–10 `run_benchmarks.py` suite yet. Scores stay **unmeasured** until a grader writes them. Live proof is the test files below — pass/fail, not a 0–10 fill. Do not invent scores.

| Axis | Score |
| --- | --- |
| Reliability | unmeasured |
| Robustness | unmeasured |
| Context efficiency | unmeasured |
| Speed | unmeasured |
| Efficiency | unmeasured |

### Reliability

Evidence: `skills/batched-change-list/scripts/test_manage_findings.py` covers add/update/delete, merge-patch semantics, batch operations, evidence records, verification runs and stage updates against a temporary tracker. `test_render.py` rejects missing proof for `verified_fixed` rather than rendering it.

### Robustness

Evidence: JSON mutations validate the whole file and write atomically; a failed render leaves the JSON authoritative and the reference says to re-render from it. The viewer reports a port conflict instead of stopping another owner. HTML-only input is read for its embedded array, so a lost JSON is recoverable.

### Context efficiency

Evidence: `get --include-evidence` keeps screenshot bytes out of the default read, `list` stays compact, and the tracker is queried by ID instead of re-reading the report. The skill defers detail to four references that load per phase.

### Speed

Evidence: one `batch --render` call applies several tracker changes and renders once, instead of a mutation-plus-render per finding.

### Efficiency

Evidence: no dependencies, no build step, no server needed to produce the page — `render.py` writes a self-contained HTML file.
