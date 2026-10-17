# Forge

Cursor Origin is the forge of record:

<https://cursor.com/codebase/gurusharan/agent-plugins>

`main` changes only through an Origin pull request. The required check is `scripts/check.py` (suite `ci`), posted by the Mini reporter. That check runs `scripts/check.py`, which runs the plugin test suites. Direct pushes to `main` are rejected. No review approval is required.

A Codex review of a pull request saves its findings on Origin: run Codex with `--output-schema <(post-review-findings --print-schema) -o verdict.json`, then `post-review-findings --repo gurusharan/agent-plugins --pr <n> --findings verdict.json` (Mini: `~/Documents/remote-claude/active/apps/orn-origin-work/bin/post-review-findings`).
It posts one comment review with an inline comment per finding that has a file and line, skips findings already posted, and exits 1 when the post fails. It posts as the Origin CLI user until orn-review is installed on this repository.

GitHub <https://github.com/ingpoc/agent-plugins> is an outbound mirror. A merge to Origin `main` is pushed to GitHub `main` by the Mini mirror job. Do not merge to GitHub `main` by hand.

Install still uses the GitHub URL. Marketplace registration is unchanged. Plugin versions stay put unless installed package contents change.
