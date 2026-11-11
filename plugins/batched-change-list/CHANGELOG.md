# Changelog: batched-change-list

## 0.1.4

- Add the Fast cycle (triage once, parallel fixes, one install and one combined live check per build) to fix-and-verify.

## 0.1.1

- Store optional merge authorization in the product repository’s agent instructions, where it survives plugin refreshes.

## 0.1.0

- First portable package: the `batched-change-list` skill with four references, `manage_findings.py`, `render.py`, `serve.py`, the HTML template, an example tracker and 32 tests.
- Portability: script commands are skill-relative (no `~/.agents/skills/...` paths), the six external principle links are replaced by bundled `references/principles.md`, and the package has no dependency on other skills.
- Merge policy moved out of the skill: this package ships no standing merge authorization. An owner records one in the product repository’s agent instructions; otherwise the skill proposes the merge and asks.
