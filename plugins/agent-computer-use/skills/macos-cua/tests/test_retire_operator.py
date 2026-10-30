import importlib.util
import ast
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/legacy_operator_migration.py"
spec = importlib.util.spec_from_file_location("legacy_operator_migration", SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class RetireOperatorTest(unittest.TestCase):
    def test_workflow_and_scripts_do_not_load_the_removed_helper(self):
        scripts = SCRIPT.parent
        source = (scripts / "workflow.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in {"cmd_preflight", "cmd_closeout"} or "doctor" in node.name
            ):
                body = ast.get_source_segment(source, node)
                for forbidden in ("operator_ui", "_operator_ui", "operator_update", "glide_operator", "com.macos-cua.operator"):
                    self.assertNotIn(forbidden, body)
        self.assertIn("retire_legacy_operator()", source)
        for path in scripts.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    self.assertNotIn("operator_ui", [alias.name for alias in node.names], str(path))
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "operator_ui", str(path))

    def test_moves_once_and_preserves_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trash = root / "trash"
            trash.mkdir()
            (trash / "state").write_text("existing")
            state = root / "state"
            state.write_text("legacy")
            app = root / "app"
            app.mkdir()
            (app / "file").write_text("app")
            plist = root / "service.plist"
            plist.write_text("plist")
            lock = root / "state.lock"
            lock.write_text("lock")
            log_dir = root / "logs"
            log_dir.mkdir()
            (log_dir / "operator.log").write_text("log")
            with mock.patch.object(migration, "TRASH", trash), mock.patch.object(
                migration, "LEGACY_PATHS", (state, app, plist, lock, log_dir)
            ), mock.patch.object(migration.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.side_effect = [mock.Mock(returncode=0), mock.Mock(returncode=3)]
                first = migration.retire_legacy_operator()
                second = migration.retire_legacy_operator()
            self.assertTrue(first["ok"])
            self.assertTrue(first["unloaded"])
            self.assertEqual(len(first["trashed"]), 5)
            self.assertEqual((trash / "state").read_text(), "existing")
            self.assertEqual((trash / "state 1").read_text(), "legacy")
            self.assertEqual((trash / "logs" / "operator.log").read_text(), "log")
            self.assertTrue(second["already_retired"])
            self.assertTrue(second["ok"])
            self.assertEqual(second["trashed"], [])
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][0:2], ["launchctl", "bootout"])

    def test_no_files_still_unloads_and_not_loaded_is_success(self):
        for code in (3, 5, 113):
            with self.subTest(code=code), mock.patch.object(migration, "LEGACY_PATHS", ()), \
                    mock.patch.object(migration.subprocess, "run", return_value=mock.Mock(returncode=code)) as run:
                result = migration.retire_legacy_operator()
                self.assertTrue(result["ok"])
                self.assertTrue(result["already_retired"])
                run.assert_called_once()

    def test_loaded_service_without_files_is_still_unloaded(self):
        with mock.patch.object(migration, "LEGACY_PATHS", ()), \
                mock.patch.object(migration.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            result = migration.retire_legacy_operator()
        self.assertTrue(result["ok"])
        self.assertTrue(result["unloaded"])
        self.assertFalse(result["already_retired"])
        run.assert_called_once()

    def test_move_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "state"
            source.write_text("legacy")
            with mock.patch.object(migration, "TRASH", root / "trash"), \
                    mock.patch.object(migration, "LEGACY_PATHS", (source,)), \
                    mock.patch.object(migration.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                    mock.patch.object(migration.shutil, "move", side_effect=OSError("move failed")):
                result = migration.retire_legacy_operator()
            self.assertFalse(result["ok"])
            self.assertIn("move failed", result["reason"])
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
