import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NeedsYouWrapTests(unittest.TestCase):
    def test_narrow_needs_you_reason_wraps_and_is_short(self):
        template = (ROOT / "ui" / "template.html").read_text()
        self.assertIn("overflow-wrap:anywhere", template)
        self.assertIn("flex-wrap:wrap", template)
        app = (ROOT / "ui" / "app.js").read_text()
        self.assertIn("No attempts", app)
        self.assertNotIn("No attempt recorded", app)
        dashboard = (ROOT / "ui" / "dashboard.html").read_text()
        self.assertIn("No attempts", dashboard)
        self.assertIn("overflow-wrap:anywhere", dashboard)


if __name__ == "__main__":
    unittest.main()
