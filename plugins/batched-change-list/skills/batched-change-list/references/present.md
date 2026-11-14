# Present

Run `bcl start` a second time to record Present. The live tracker is the source; `bcl` snapshots JSON and HTML in the home worktree. The HTML page is the user's only view of progress.

The renderer validates IDs, sources and canonical statuses, requires a verification summary and references for `verified_fixed`, safely embeds the data and local cropped PNG/JPEG/WebP/GIF evidence, and derives navigation, selected panel, badges and counts from the same records. `evidence` entries use `src` (path relative to the JSON), `alt`, `caption`, and optional `full` (HTTP(S) URL or path relative to the HTML). Missing proof is an error, not proof of acceptance; the agent must judge whether references actually establish it. Never paste personal data or secrets into a public report.

Show one selected issue at a time with a short problem, the reason for its latest check outcome, and a relevant crop. Label an older result as previous when the current install is unchecked. Put provenance, older evidence and scope in `details`. Verify navigation and the selected panel at desktop and mobile widths, then link the page.

Keep current status, build and acceptance in the current check and summary. Label older observations by build and date, never as the current install. Consolidate repeated explanations; retain distinct evidence and verification history. Review stale labels, repeated details and obsolete user-test instructions before rendering. Do not automatically delete evidence.

Use `bcl` stages (or `set-activity` in a tracker batch) as each phase or issue-group step starts; it updates the stage, affected issue statuses, and one timestamped `Now` line. Stored plugin inventory is labeled `As recorded`; live discovery reads installed copies at render time and uses recorded versions only for plugins it cannot find. For renderer flags, inventory overrides, and recovery when a render fails, see [tracker.md](tracker.md) and [viewer.md](viewer.md).
