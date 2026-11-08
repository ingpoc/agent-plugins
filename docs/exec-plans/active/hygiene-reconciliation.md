# Hygiene reconciliation

## Usage and ownership

`hygiene check` builds one advisory map: local Git inventory plus bounded read-only Origin PR and Origin/GitHub ref observations. The map shows whether each observation is current or unknown; a snapshot is never permission to delete. `safe-delete <path> --yes` independently archives and verifies the target, checks configured remotes, and invokes normal Git worktree removal without a force fallback. The Origin mirror job remains the owner of GitHub branch deletion.

## Design

`observe_remote(repo, origin, github) -> {origin_refs, github_refs, open_prs, observed_at}` returns explicit unknowns on errors or incomplete PR results. The existing `git_snapshot` attaches these observations; the tree classifies an item as a cleanup candidate only with a matching live Origin main, a complete open/draft PR listing, no active PR, and clean local state. Remote mirror differences are displayed as observations, never as confirmed cleanup. Deletion is a separate action that must recheck identity and state.

## Rationale

Two independently considered approaches were a separate on-demand reconciliation command and bounded observation integrated into the existing map. The latter keeps one inventory and makes freshness visible where the user already looks. Unlike the separate-command option, regular map generation incurs bounded network calls; failure remains visible as unknown and does not fail the hygiene check. Neither design treats an old observation as a deletion receipt. Future cleanup automation must revalidate immediately before mutation.

## Verification

Test offline and partial PR observations, active PR protection, live/cached main mismatch, mirror lag, errors, and dirty/ignored worktree refusal. Preserve existing atomic map publication and unrelated machine configuration. Observe the installed map and deletion command with isolated fixtures before claiming live behavior.
