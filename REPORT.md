# PAGE-05

- Upcoming unmet Fixes gate uses a 14px marker with a 2px progress ring and no gate emphasis class.
- Current stages keep their 14px, 2.5px ring and Now marker; met gates keep the 22px done marker.
- Rendered-page regression checks cover the node classes and actual SVG marker dimensions/strokes.
- Verified with bytecode disabled: `python3 -m unittest scripts.test_render` (37 tests passed).
- Scope: `render.py`, `test_render.py`, and this report.
