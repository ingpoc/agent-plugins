#!/usr/bin/env python3
# Standard-library modules remain public compatibility patch surfaces.
# ruff: noqa: F401, F821
"""Stable CLI and compatibility facade for modular macOS computer use.

The implementation is split by responsibility across ``runtime_*.py`` files.
This facade preserves the historical import surface while ensuring delegated
functions observe facade-level monkeypatches used by tests and integrations.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


SKILL_ROOT = Path(__file__).resolve().parent.parent
CUA_DRIVER = os.environ.get("CUA_DRIVER", os.path.expanduser("~/.local/bin/cua-driver"))
MIN_CUA_DRIVER_VERSION = (0, 8, 3)
CUA_SESSION = os.environ.get("MACOS_CUA_SESSION", "macos-cua")
CURSOR_ICON = os.environ.get(
    "MACOS_CUA_CURSOR_ICON",
    str(SKILL_ROOT / "assets" / "pointer-shape-animated.svg"),
)
CACHE_DIR = os.environ.get(
    "MACOS_CUA_CACHE_DIR", os.path.expanduser("~/.cache/macos-cua")
)
os.makedirs(CACHE_DIR, exist_ok=True)
SCREENSHOT_DIR = os.path.join(CACHE_DIR, "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
CURSOR_RASTER = os.path.join(CACHE_DIR, "hermes-pointer.png")
VISION_OCR_SOURCE = os.path.join(Path(__file__).parent, "vision-window-ocr.swift")
VISION_OCR_BINARY = os.path.join(CACHE_DIR, "vision-window-ocr")
HERMES_CHROME_ROOT = Path(
    os.environ.get(
        "HERMES_CHROME_RUNTIME_ROOT",
        str(Path.home() / "plugins" / "hermes-chrome-cursor-wip"),
    )
)
HERMES_CUA_GUARD = Path(
    os.environ.get(
        "HERMES_CHROME_CUA_GUARD",
        HERMES_CHROME_ROOT / "scripts/check-cua-coexistence.py",
    )
)
HERMES_USER_DATA_DIR = Path(
    os.environ.get(
        "HERMES_CHROME_USER_DATA_DIR",
        HERMES_CHROME_ROOT / "run/chrome-user-data",
    )
).resolve()

_CURSOR_CONFIGURED = False
_DISPLAYS_MOD = None
_OPERATOR_UI_MOD = None
_PLAN_CONTRACT_MOD = None
_CLI_PARSER_MOD = None
_NATIVE_INPUT_MOD = None
_NATIVE_TEXT_POINTER_MOD = None
_HERMES_RUNTIME_CLAIM = None
_VISUAL_FOCUS_MOD = None
_VISUAL_FOCUS_LEASE = None

_MUTABLE_STATE = {
    "_CURSOR_CONFIGURED",
    "_DISPLAYS_MOD",
    "_OPERATOR_UI_MOD",
    "_PLAN_CONTRACT_MOD",
    "_CLI_PARSER_MOD",
    "_NATIVE_INPUT_MOD",
    "_NATIVE_TEXT_POINTER_MOD",
    "_HERMES_RUNTIME_CLAIM",
    "_VISUAL_FOCUS_MOD",
    "_VISUAL_FOCUS_LEASE",
}
_RUNTIME_FILES = (
    "runtime_telemetry.py",
    "runtime_coexistence.py",
    "runtime_driver.py",
    "runtime_apps.py",
    "runtime_snapshot.py",
    "runtime_capture.py",
    "runtime_vision.py",
    "runtime_labels.py",
    "runtime_pointer.py",
    "runtime_pointer_actions.py",
    "runtime_accessibility.py",
    "runtime_plan.py",
    "runtime_cli.py",
)
_RUNTIME_MODULES: list[tuple[ModuleType, set[str]]] = []


def _load_runtime(path: Path) -> tuple[ModuleType, set[str]]:
    """Load one implementation module and identify definitions it owns."""
    module_name = f"_macos_cua_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load macos-cua runtime module: {path}")
    module = importlib.util.module_from_spec(spec)
    module.__dict__.update(
        {
            name: value
            for name, value in globals().items()
            if not name.startswith("__")
        }
    )
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    owned = {
        name
        for name, value in vars(module).items()
        if callable(value) and getattr(value, "__module__", None) == module_name
    }
    return module, owned


def _sync_dependencies(module: ModuleType, owned: set[str]) -> None:
    """Expose current facade dependencies to a delegated implementation."""
    facade = globals()
    for name, value in facade.items():
        if not name.startswith("__"):
            vars(module)[name] = value


def _sync_mutable_state(module: ModuleType) -> None:
    """Round-trip the few intentional process-wide caches and leases."""
    facade = globals()
    for name in _MUTABLE_STATE:
        if name in vars(module):
            facade[name] = vars(module)[name]


def _delegate(
    module: ModuleType, owned: set[str], name: str, target: Callable[..., Any]
) -> Callable[..., Any]:
    """Create a compatibility wrapper with live dependency injection."""

    @functools.wraps(target)
    def delegated(*args: Any, **kwargs: Any) -> Any:
        _sync_dependencies(module, owned)
        try:
            return target(*args, **kwargs)
        finally:
            _sync_mutable_state(module)

    delegated.__module__ = __name__
    return delegated


def _compose_runtime() -> None:
    """Publish all runtime definitions through the stable facade."""
    scripts = Path(__file__).parent
    facade = globals()
    for filename in _RUNTIME_FILES:
        module, owned = _load_runtime(scripts / filename)
        _RUNTIME_MODULES.append((module, owned))
        for name, value in vars(module).items():
            if name.isupper() and name not in facade:
                facade[name] = value
        for name in owned:
            value = vars(module)[name]
            facade[name] = (
                _delegate(module, owned, name, value)
                if callable(value) and not isinstance(value, type)
                else value
            )


_compose_runtime()


if __name__ == "__main__":
    main()
