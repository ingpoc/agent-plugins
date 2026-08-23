#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1

python3 - "$SKILL_DIR" <<'PY'
import ast
from pathlib import Path
import sys

root = Path(sys.argv[1])
for path in sorted(root.rglob("*.py")):
    ast.parse(path.read_text(), filename=str(path))
non_runtime = {"fast_path.py", "validate-macos-cua.py"}

def source_lines(path):
    return sum(
        bool(line.strip()) and not line.lstrip().startswith("#")
        for line in path.read_text().splitlines()
    )

oversized = {
    str(path.relative_to(root)): source_lines(path)
    for path in sorted((root / "scripts").glob("*.py"))
    if path.name not in non_runtime and source_lines(path) > 600
}
oversized.update(
    {
        str(path.relative_to(root)): len(path.read_text().splitlines())
        for path in sorted((root / "operator").glob("*.swift"))
        if len(path.read_text().splitlines()) > 600
    }
)
if oversized:
    raise SystemExit(f"production modules exceed 600 source lines: {oversized}")
print("python syntax: ok")
PY

python3 -m unittest discover -s "$SKILL_DIR/tests" -v
xcrun swiftc -typecheck -parse-as-library "$SKILL_DIR/scripts/vision-window-ocr.swift"
xcrun swiftc -typecheck "$SKILL_DIR/tests/fixtures/drag_fixture.swift"
python3 "$SKILL_DIR/scripts/validate-macos-cua.py"
CODEX_SKILLS_ROOT="${MACOS_CUA_CODEX_SKILLS_ROOT:-$HOME/.codex/skills}"
python3 "$CODEX_SKILLS_ROOT/.system/skill-creator/scripts/quick_validate.py" "$SKILL_DIR"
python3 "$CODEX_SKILLS_ROOT/create-skill/scripts/audit.py" "$SKILL_DIR" --strict

echo '{"ok":true,"skill":"macos-cua","validation":"static"}'
