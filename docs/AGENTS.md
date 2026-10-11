# Forge

Cursor Origin is the forge of record:

<https://cursor.com/codebase/gurusharan/agent-plugins>

`main` changes only through an Origin pull request. The required check is `scripts/check.py` (suite `ci`), posted by the Mini reporter. That check runs `scripts/check.py`, which runs the plugin test suites. Direct pushes to `main` are rejected. No review approval is required.

GitHub <https://github.com/ingpoc/agent-plugins> is an outbound mirror. A merge to Origin `main` is pushed to GitHub `main` by the Mini mirror job. Do not merge to GitHub `main` by hand.

Install still uses the GitHub URL. Marketplace registration is unchanged. Plugin versions stay put unless installed package contents change.
