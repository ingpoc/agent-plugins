"""Regression coverage for frozen helpers with plugin.json beside their scripts."""

import importlib.util
import shutil
import sys
from pathlib import Path


def test_plugin_root_includes_script_directory(tmp_path):
    scripts = tmp_path / "Resources"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts"
    for name in (
        "compact_backend.py",
        "compact_instructions.txt",
        "compact_form_fills.py",
        "compact_verification.py",
        "fail_feedback.py",
        "state_delta.py",
    ):
        shutil.copy(source / name, scripts / name)
    (scripts / "plugin.json").write_text("{}")

    spec = importlib.util.spec_from_file_location("compact_backend_frozen_test", scripts / "compact_backend.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(scripts))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))

    assert module.PLUGIN_ROOT == scripts
