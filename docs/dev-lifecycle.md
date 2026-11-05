# Development lifecycle (every harness)

Guru 2026-11-03 15:12 IST: "it has to be followed by all harness". Applies to every harness working in agent-plugins (and its plugins ACU, comet-control and context-ledger). Paths come from `~/.agents/machine.toml`; don't hard-code them.

## 1. Develop
1. Docs first: update the plugin `SKILL.md`, `references/` or `AGENTS.md` in the same PR as the code change. The PR body states the user-visible rule.
2. Hygiene green: `python3 ~/.agents/bin/hygiene.py check --json`. Any FAIL item you own (your branch, worktree or local-only commit) must be fixed or reported first. Never delete someone else's item.
3. One task = one branch = one worktree: `git -C <checkout_root>/agent-plugins fetch origin-cursor && git worktree add <worktree_root>/agent-plugins/<task> -b <harness>/<task> origin-cursor/main`. Not `~/code/wt`, not a second clone. The canonical checkout is `<checkout_root>/agent-plugins`.
4. Local gate: `scripts/check.py` (never two full runs at once on the Mini, and not while the CI reporter is running one: the Swift suite flakes under parallel load), and the pre-push hook runs on push. Tests that touch the live island run with `ACU_ISLAND_ACTIVITY=0`.

## 2. Maintain
- Whatever is installed on the Mini (the app, `~/.agents/plugins/*` or harness caches) must come from Origin main or an open PR head, through `scripts/release_agent_computer_use.py`. Never hand-patch hashed plugin dirs.
- Every open PR has a live owner. Branches and worktrees untouched for 7+ days go to the owner with last commit and status.
- After a merge, in the same turn: `git worktree remove`, `git branch -d`, and delete the branch on Origin (the mirror follows). Abandoned work: make a verified `git bundle` under `<archive>/<date>-<topic>/` first, then retire it through the hygiene deletion gate. Retiring needs Guru's OK.

## Enforcement
Rules that machines check, so the 2026-11-03 cleanup does not recur:
- **Pre-push placement** (`scripts/placement.py`, run by `.githooks/pre-push`): a push must come from `<checkout_root>/agent-plugins` or a worktree of it under `<worktree_root>/agent-plugins/` (or a harness root in `[exceptions].worktree_roots`). A second clone or a stray worktree is blocked. `AGENT_PLUGINS_ALLOW_PLACEMENT=1` pushes once anyway; say why in the PR. A no-op in CI or without `~/.agents/machine.toml`. Install with `scripts/install-hooks.sh`.
- **Release hygiene gate**: `release_agent_computer_use.py` runs `hygiene check --json` first and stops if `worktree-placement`, `duplicate-clones` or `local-only-work` FAILs for an agent-plugins checkout. `--hygiene-override` releases anyway and records the failures in the report's `hygiene` field.
- **Branch deletion mirroring**: the Origin sync job (`agent-plugins-origin-sync`, outside this repo) deletes a GitHub branch once Origin has deleted it, it is not an open or draft PR head, and its commits are in Origin main. Unmerged GitHub-only branches are reported as `KEEP`, never deleted.
- **Map page**: every `hygiene check` (plain, `--strict`, `--json`; skip with `--no-map`) refreshes `~/.agents/hygiene/map.html`, an interactive tree rooted at the Mac Mini: every clone under the scan roots by repo, with worktrees, branches and extra clones coloured fail / warn / stale (safe to sweep) / ok, and a side panel with the plain-English reason for each check. The git scan runs on a thread while the checks run; the page is rewritten atomically only when the snapshot hash (`map.sha256` beside it) changes. A map failure prints one stderr line and never changes the check's output or exit code. `hygiene map` rewrites the page now and opens it (`--no-open` to skip). Source: `scripts/hygiene.py` and `scripts/hygiene_map.py`; `scripts/install-hygiene.sh` installs both into `~/.agents/bin/` (backing up a differing `hygiene.py` as `hygiene.py.bak-YYYYMMDD`) and links `~/.local/bin/hygiene`. Run it in the same turn as the merge.
- **After every merge**: remove the worktree, `git branch -d`, delete the Origin branch (section 2). Retiring unmerged work still needs a verified bundle and Guru's OK.

## 3. Release
1. Bump the SemVer in every manifest and `Info.plist` together. `check.py` release-policy enforces this; versions never go backwards, so a revert ships as the next number.
2. Open the PR, get CI green, do a normal `origin pr merge` (no admin, no force-push).
   - Known flakes: `IslandGateColdStartTests`. Add a test here only after an isolated rerun has passed. When the only red check on a PR is a listed known flake and its isolated rerun passes, run `agent-plugins-origin-ci once <pr> --force` right away to refresh the stale Origin check. It needs no extra sign-off, because it only reruns CI and changes nothing.
3. Before tagging, check that no revert, relay or spec change is still pending. If one landed after the merge, ship it first as the next version.
4. Tag `agent-computer-use-v<ver>` on the merge commit and push it to `origin-cursor` only. The Origin mirror job copies main and tags to GitHub.
5. Build from main the normal way, so you get a signed, notarized and stapled DMG: `release_agent_computer_use.py --report …`, `make-dmg-clean.sh`, then `stapler validate` and `spctl -a -t open --context context:primary-signature`. The release checks for a default microphone input before building when device discovery is available; an unknown result does not replace the installed-app microphone check.
6. Assets: `scripts/release_assets.py build --dmg … --out …` (SHA256SUMS, manifest.json, install-mac.sh).
7. GitHub release: `GH_TOKEN="$(gh auth token -u ingpoc)" gh release create <tag> -R ingpoc/agent-plugins --verify-tag --latest …`. Never `gh auth switch`, never print the token. Afterwards `gh auth status` must still show the default account as active.
8. Release-check flakes (for example `live-fd` while other clients are connected, or a known flake from step 2 under load): rerun that check alone, and note both results in the release report. A check that fails again on its own blocks the release.

## 4. Sync
- Origin is the source of truth. GitHub `ingpoc/agent-plugins` is a mirror; never push to it directly.
- Check the mirror: `git ls-remote origin refs/heads/main refs/tags/<tag>` matches Origin.
- Live manifest: `https://github.com/ingpoc/agent-plugins/releases/latest/download/manifest.json` shows the new version. Download the DMG and check its SHA.
- Sites: set the version, build, SHA and size in `agent-plugins-site/site/build.py` (plus the fallback label in `static/home/index.html`) and in `island-site/release.json`. Rebuild both online (`site/build.py`, `island-site/build.py`). The redeploy is private and runs under the rpg.ramashram account; deploy only with Guru's go.

## 5. Refresh (same turn as the release)
Each harness updates through its own native mechanism (per-harness commands: the `agent-plugins` skill `references/harness-matrix.md`), then makes one real ACU `state` call from `~/code/acu-release/empty-probe`:
- Grok Build: `grok plugin marketplace update` then `grok plugin update agent-computer-use`, then `grok -p "$P" --always-approve`.
- Codex: the release script refreshes `~/.agents/plugins`; `codex plugin add agent-computer-use@personal`; `codex exec --skip-git-repo-check "$P"`. Check the cache version.
- Cursor CLI: a new `agent -p "$P" --approve-mcps --force` (it reads the release-script copy).
- Cursor IDE: marketplace index at the main commit (the mirror job runs `refresh_cursor_marketplace_index.sh`), Refresh, reload MCP, one call.
- Claude Code: `claude plugin marketplace update ingpoc-agent-plugins` then `claude plugin update agent-computer-use@ingpoc-agent-plugins` (new session after), then `claude -p "$P"` from empty-probe.
- Grok Bot: `macos-cua.py state Finder` on the Mini, and sync the box skill copies from main.
The release isn't done until every row has a result in the report.

## How each harness gets this rule
| Harness | Loads | Source |
|---|---|---|
| Codex | `~/.codex/AGENTS.md`, then each `AGENTS.md` from the git root down to cwd | developers.openai.com/codex/guides/agents-md |
| Cursor IDE/CLI | Root and nested `AGENTS.md`, `.cursor/rules/*.mdc`, User Rules (settings) | cursor.com/docs/context/rules |
| Claude Code | `CLAUDE.md` from cwd and its parents. `AGENTS.md` is read only when no `CLAUDE.md` exists (v2.1.277+); `~/.claude/CLAUDE.md` doesn't block it | docs.anthropic.com/en/docs/claude-code/memory |
| Grok Build | `AGENTS.md`/`CLAUDE.md` per directory (folder trust required), `.grok/.claude/.cursor/rules` | ~/.grok/docs/user-guide/12-project-rules.md |
| Grok Bot | The `agent-plugins` skill (box `/home/box/agent-data/workflows/` and the Mini `~/.agents/skills/`) | — |
