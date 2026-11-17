import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("previous_report.py")


class PreviousReportTests(unittest.TestCase):
    def test_recovers_deleted_report_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            page = root / "docs/triage/pr-42.html"
            page.parent.mkdir(parents=True)
            page.write_text("<html>original</html>")
            git("add", ".")
            git("commit", "-qm", "report")
            page.unlink()
            git("add", "-u")
            git("commit", "-qm", "remove")
            output = root / "old.html"
            run = lambda pr: subprocess.run([sys.executable, str(SCRIPT), str(pr), str(output)], cwd=root, capture_output=True, text=True)
            first = run(42)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(output.read_text(), "<html>original</html>")
            self.assertIn("from", first.stderr)
            self.assertNotEqual(run(42).returncode, 0)
            self.assertEqual(output.read_text(), "<html>original</html>")
            missing = run(43)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("no committed report", missing.stderr)


if __name__ == "__main__":
    unittest.main()
