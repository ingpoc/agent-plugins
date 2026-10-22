"""Discovery and allow-list for scripts/check.py. No plugin imports."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

EXCLUDE_PARTS = {".git", ".build", "node_modules", "__pycache__", ".venv"}
MIN_REASON = 10
_WHEN = re.compile(
    r"^(?:platform == [A-Za-z0-9_]+|platform != [A-Za-z0-9_]+|ci|not ci|"
    r"env:[A-Za-z_][A-Za-z0-9_]*(?:=[^\s]+)?|not env:[A-Za-z_][A-Za-z0-9_]*)$"
)
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "check_test_allowlist.json"


class AllowlistError(ValueError):
    pass


@dataclass(frozen=True)
class SkipRule:
    kind: str
    id: str
    reason: str
    when: str | None


@dataclass(frozen=True)
class Allowlist:
    rules: tuple[SkipRule, ...]

    def match(self, kind: str, rule_id: str, platform: str, env: os._Environ[str] | dict[str, str]) -> SkipRule | None:
        for rule in self.rules:
            if rule.kind == kind and rule.id == rule_id and when_matches(rule.when, platform=platform, env=env):
                return rule
        return None


@dataclass(frozen=True)
class PythonRun:
    path: Path
    cwd: Path
    mode: str
    start: Path | None


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def defines_testcase(path: Path) -> bool:
    """True when the file defines a unittest.TestCase, or cannot be parsed.

    Live scripts named test_*.py that only provide main() are not unit tests.
    A syntax error stays in the run set so the runner fails closed.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(_base_name(base) == "TestCase" for base in node.bases):
            return True
    return False


def _candidate_test_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in ("plugins", "scripts"):
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("test_*.py"):
            if not path.is_file():
                continue
            if any(part in EXCLUDE_PARTS for part in path.relative_to(root).parts):
                continue
            found.append(path)
    return sorted(found, key=lambda item: item.relative_to(root).as_posix())


def python_test_files(root: Path) -> list[Path]:
    return [path for path in _candidate_test_files(root) if defines_testcase(path)]


def script_files(root: Path, plugin_names: set[str] | None = None) -> list[str]:
    rows: list[str] = []
    for path in _candidate_test_files(root):
        if not _in_scope(path, root, plugin_names):
            continue
        if defines_testcase(path):
            continue
        rows.append(path.relative_to(root).as_posix())
    return rows


INTEGRATION_ENV = "AGENT_PLUGINS_INTEGRATION"


@dataclass(frozen=True)
class IntegrationSuite:
    path: Path
    requires: str


def integration_requires(path: Path) -> str | None:
    """Module-level INTEGRATION_REQUIRES string literal, or None."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == "INTEGRATION_REQUIRES" for t in node.targets):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
                return value.value.strip()
    return None


def integration_suites(root: Path, plugin_names: set[str] | None = None) -> list[IntegrationSuite]:
    """plugins/**/integration_*.py opt-in suites. Never part of unit discovery.

    A suite without a non-empty INTEGRATION_REQUIRES gets requires="" so the
    caller fails closed instead of reporting a nameless skip.
    """
    base = root / "plugins"
    if not base.is_dir():
        return []
    found: list[IntegrationSuite] = []
    for path in base.rglob("integration_*.py"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_PARTS for part in path.relative_to(root).parts):
            continue
        if not _in_scope(path, root, plugin_names):
            continue
        found.append(IntegrationSuite(path, integration_requires(path) or ""))
    return sorted(found, key=lambda item: item.path.relative_to(root).as_posix())


def swift_packages(root: Path) -> list[Path]:
    base = root / "plugins"
    if not base.is_dir():
        return []
    found: list[Path] = []
    for path in base.rglob("Package.swift"):
        if any(part in EXCLUDE_PARTS for part in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if ".testTarget" not in text:
            continue
        found.append(path.parent)
    return sorted(found, key=lambda item: item.relative_to(root).as_posix())


def discover_start(path: Path) -> Path | None:
    parts = path.parts
    if "tests" not in parts:
        return None
    index = parts.index("tests")
    return Path(*parts[: index + 1])


def _in_scope(path: Path, root: Path, plugin_names: set[str] | None) -> bool:
    rel = path.relative_to(root)
    if rel.parts[0] == "scripts":
        return True
    if rel.parts[0] != "plugins" or len(rel.parts) < 2:
        return False
    if plugin_names is None:
        return True
    return rel.parts[1] in plugin_names


def python_runs(root: Path, plugin_names: set[str] | None = None) -> list[PythonRun]:
    runs: list[PythonRun] = []
    for path in python_test_files(root):
        if not _in_scope(path, root, plugin_names):
            continue
        rel = path.relative_to(root)
        if rel.parts[0] == "scripts":
            runs.append(PythonRun(path, root, "module", None))
            continue
        plugin = root / "plugins" / rel.parts[1]
        start = discover_start(path)
        if start is None:
            runs.append(PythonRun(path, plugin, "module", None))
        else:
            runs.append(PythonRun(path, plugin, "discover", start))
    return runs


def module_id(root: Path, path: Path) -> str:
    return "module:" + path.relative_to(root).as_posix()


def swift_id(root: Path, package: Path) -> str:
    return "swift:" + package.relative_to(root).as_posix()


def when_matches(expr: str | None, *, platform: str, env: os._Environ[str] | dict[str, str]) -> bool:
    if expr is None:
        return True
    if expr == "ci":
        return env.get("CI") == "true"
    if expr == "not ci":
        return env.get("CI") != "true"
    if expr.startswith("platform == "):
        return platform == expr[len("platform == ") :]
    if expr.startswith("platform != "):
        return platform != expr[len("platform != ") :]
    if expr.startswith("not env:"):
        return expr[len("not env:") :] not in env
    if expr.startswith("env:"):
        body = expr[len("env:") :]
        if "=" in body:
            key, value = body.split("=", 1)
            return env.get(key) == value
        return body in env
    raise AllowlistError(f"unsupported when expression: {expr}")


def load_allowlist(path: Path) -> Allowlist:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AllowlistError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AllowlistError(f"{path}: allow list must be an object")
    extra = set(data) - {"version", "unittest_skips", "harness_skips"}
    if extra:
        raise AllowlistError(f"{path}: unknown keys: {', '.join(sorted(extra))}")
    if data.get("version") != 1:
        raise AllowlistError(f"{path}: version must be 1")
    rules: list[SkipRule] = []
    seen: set[str] = set()
    for kind, key in (("unittest", "unittest_skips"), ("harness", "harness_skips")):
        entries = data.get(key, [])
        if not isinstance(entries, list):
            raise AllowlistError(f"{path}: {key} must be a list")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise AllowlistError(f"{path}: {key}[{index}] must be an object")
            unknown = set(entry) - {"id", "reason", "when"}
            if unknown:
                raise AllowlistError(f"{path}: {key}[{index}] unknown keys: {', '.join(sorted(unknown))}")
            rule_id = entry.get("id")
            reason = entry.get("reason")
            when = entry.get("when")
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise AllowlistError(f"{path}: {key}[{index}] id must be a non-empty string")
            if rule_id in seen:
                raise AllowlistError(f"{path}: duplicate id {rule_id}")
            seen.add(rule_id)
            if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON:
                raise AllowlistError(f"{path}: {rule_id} reason must be at least {MIN_REASON} characters")
            if when is not None and (not isinstance(when, str) or not _WHEN.fullmatch(when)):
                raise AllowlistError(f"{path}: {rule_id} when must use platform, ci, or env:*")
            rules.append(SkipRule(kind, rule_id, reason.strip(), when))
    return Allowlist(tuple(rules))


def _counts(found: int, run: int, allowed: int, unallowed: int) -> dict[str, int]:
    return {
        "found": found,
        "run": run,
        "skipped_allowed": allowed,
        "skipped_unallowed": unallowed,
    }


def audit(
    root: Path,
    *,
    allowlist: Allowlist | None = None,
    allowlist_path: Path | None = None,
    platform: str | None = None,
    plugin_names: set[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    loaded = allowlist if allowlist is not None else load_allowlist(allowlist_path or DEFAULT_ALLOWLIST)
    host = platform or sys.platform
    surroundings = env if env is not None else os.environ
    py_run = py_allowed = py_unallowed = 0
    unallowed_ids: list[str] = []
    files = [path for path in python_test_files(root) if _in_scope(path, root, plugin_names)]
    for path in files:
        rule_id = module_id(root, path)
        rule = loaded.match("harness", rule_id, host, surroundings)
        if rule is not None:
            py_allowed += 1
        else:
            py_run += 1
    packages = [
        package
        for package in swift_packages(root)
        if _in_scope(package / "Package.swift", root, plugin_names)
    ]
    swift_run = swift_allowed = swift_unallowed = 0
    for package in packages:
        rule_id = swift_id(root, package)
        if host == "darwin":
            swift_run += 1
            continue
        rule = loaded.match("harness", rule_id, host, surroundings)
        if rule is not None:
            swift_allowed += 1
        else:
            swift_unallowed += 1
            unallowed_ids.append(rule_id)
    return {
        "python": _counts(len(files), py_run, py_allowed, py_unallowed),
        "swift": _counts(len(packages), swift_run, swift_allowed, swift_unallowed),
        "harness_skips": unallowed_ids,
        "script_files": script_files(root, plugin_names),
        "integration_suites": [
            suite.path.relative_to(root).as_posix()
            for suite in integration_suites(root, plugin_names)
        ],
    }
