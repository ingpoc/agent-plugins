#!/usr/bin/env python3
"""Run unittest and fail when a skip is not allow-listed."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import check_inventory


def _iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _load_module(path: Path) -> unittest.TestSuite:
    name = path.stem
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return unittest.defaultTestLoader.loadTestsFromModule(module)


def _load(mode: str, start: str | None, pattern: str | None, module_path: str | None) -> unittest.TestSuite:
    if mode == "discover":
        assert start is not None and pattern is not None
        return unittest.defaultTestLoader.discover(start, pattern=pattern, top_level_dir=start)
    assert module_path is not None
    return _load_module(Path(module_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run unittest and fail on unlisted skips.")
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("-q", action="store_true")
    sub = parser.add_subparsers(dest="mode", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("-s", dest="start", required=True)
    discover.add_argument("-p", dest="pattern", default="test_*.py")
    module = sub.add_parser("module")
    module.add_argument("path")
    args = parser.parse_args(argv)
    try:
        allow = check_inventory.load_allowlist(args.allowlist)
        suite = _load(
            args.mode,
            getattr(args, "start", None),
            getattr(args, "pattern", None),
            getattr(args, "path", None),
        )
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1
    except Exception as exc:
        print(f"unittest load failed: {exc}", file=sys.stderr)
        return 1
    collected = list(_iter_tests(suite))
    if not collected:
        print("unittest load collected zero tests", file=sys.stderr)
        return 1
    kept: list[unittest.TestCase] = []
    for test in collected:
        rule = allow.match("harness", test.id(), sys.platform, os.environ)
        if rule is not None:
            print(f"allowed skip {test.id()}: {rule.reason}", flush=True)
            continue
        kept.append(test)
    runner = unittest.TextTestRunner(verbosity=0 if args.q else 1)
    result = runner.run(unittest.TestSuite(kept))
    unallowed = []
    for test, reason in result.skipped:
        if allow.match("unittest", test.id(), sys.platform, os.environ) is None:
            unallowed.append((test.id(), reason))
    if unallowed:
        for test_id, reason in unallowed:
            print(f"unallowed unittest skip {test_id}: {reason}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
