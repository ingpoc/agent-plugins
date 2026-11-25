#!/usr/bin/env python3
"""Observability UI stays at 16px or above and does not block browser zoom."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = [ROOT / "ui" / "template.html", ROOT / "ui" / "dashboard.html"]


def font_sizes(text):
    """Return CSS font-size values in rem or px from a stylesheet or HTML file."""
    return re.findall(r"font-size\s*:\s*([0-9.]+)(rem|px)", text, flags=re.I)


class TypographyTests(unittest.TestCase):
    def test_no_computed_text_below_16px_at_scale_one(self):
        for path in PAGES:
            sizes = font_sizes(path.read_text(encoding="utf-8"))
            self.assertTrue(sizes, f"{path.name} should declare font sizes")
            for value, unit in sizes:
                px = float(value) * 16 if unit.lower() == "rem" else float(value)
                self.assertGreaterEqual(px, 16, f"{path.name} font-size:{value}{unit} is below 16px")

    def test_viewport_does_not_block_zoom(self):
        for path in PAGES:
            html = path.read_text(encoding="utf-8")
            match = re.search(r'<meta name="viewport" content="([^"]*)"', html)
            self.assertIsNotNone(match, f"{path.name} needs a viewport tag")
            content = match.group(1)
            self.assertIn("width=device-width", content.replace(" ", ""))
            self.assertIn("initial-scale=1", content.replace(" ", ""))
            self.assertNotIn("maximum-scale", content)
            self.assertNotIn("user-scalable=no", content)
