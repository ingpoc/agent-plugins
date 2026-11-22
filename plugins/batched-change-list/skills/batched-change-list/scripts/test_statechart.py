import json
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from pathlib import Path

import statechart


class StatechartTests(unittest.TestCase):
    def test_condition_labels_sit_beside_their_edges(self):
        svg = re.search(r'<svg class="chart".*?</svg>', statechart.render()).group()
        root = ET.fromstring(svg)
        definition = statechart.definition()
        gate = definition['gate']
        cases = [('loop_label', gate['default_threshold'], ('gate','plan_fix')), ('exit_label', gate['default_threshold'], ('gate','report_rollover')), ('merge_label', gate['merge_previous_pr_threshold'], ('report_rollover','merged'))]
        for key, threshold, endpoints in cases:
            expected = gate[key].format(threshold=threshold)
            text = next(t for t in root.iter('text') if ''.join(t.itertext()) == expected)
            bx, by, width, height = map(float, text.attrib['data-box'].split(','))
            cx, cy = bx + width/2, by + height/2
            path = next(p for p in root.iter('path') if (p.attrib.get('data-from'), p.attrib.get('data-to')) == endpoints)
            tokens = re.findall(r'[MHV]|-?\d+(?:\.\d+)?', path.attrib['d'])
            x, y = map(float, tokens[1:3]); distances = []
            for i in range(3, len(tokens), 2):
                nx, ny = (float(tokens[i+1]), y) if tokens[i] == 'H' else (x, float(tokens[i+1]))
                dx = max(min(x,nx)-cx, 0, cx-max(x,nx)); dy = max(min(y,ny)-cy, 0, cy-max(y,ny))
                distances.append((dx*dx + dy*dy)**0.5)
                x, y = nx, ny
            self.assertLessEqual(min(distances), 40, key)

    def test_edges_do_not_cross_labels(self):
        svg = re.search(r'<svg class="chart".*?</svg>', statechart.render()).group()
        root = ET.fromstring(svg)
        boxes = [tuple(map(float, t.attrib['data-box'].split(','))) for t in root.iter('text') if 'data-box' in t.attrib]
        for path in (p for p in root.iter('path') if p.attrib.get('class') == 'edge'):
            tokens = re.findall(r'[MHV]|-?\d+(?:\.\d+)?', path.attrib['d'])
            x, y = map(float, tokens[1:3]); i = 3
            while i < len(tokens):
                command, value = tokens[i], float(tokens[i+1]); i += 2
                nx, ny = (value, y) if command == 'H' else (x, value)
                for bx, by, width, height in boxes:
                    crosses = (x == nx and bx <= x <= bx+width and min(y,ny) <= by+height and max(y,ny) >= by) or (y == ny and by <= y <= by+height and min(x,nx) <= bx+width and max(x,nx) >= bx)
                    self.assertFalse(crosses, (path.attrib['d'], (bx,by,width,height)))
                x, y = nx, ny
        with patch.object(statechart, 'edge_crosses_box', return_value=True):
            with self.assertRaisesRegex(ValueError, 'chart edge crosses label'):
                statechart.render()

    def test_chart_edges_are_svg_paths_with_arrowheads(self):
        chart = statechart.render()
        self.assertGreaterEqual(chart.count('class="edge"'), 10)
        self.assertNotRegex(chart, r'[↩↶→↓]')
        self.assertGreaterEqual(chart.count('marker-end="url(#arrow)"'), 10)

    def test_chart_labels_have_no_markdown(self):
        chart = statechart.render()
        self.assertNotIn('`', chart)
        self.assertNotRegex(chart, r'\*\*')

    def test_chart_labels_do_not_overlap(self):
        svg = re.search(r'<svg class="chart".*?</svg>', statechart.render()).group()
        root = ET.fromstring(svg)
        boxes = [tuple(map(float, text.attrib['data-box'].split(','))) for text in root.iter('text') if 'data-box' in text.attrib]
        boxes += [tuple(float(rect.attrib[k]) for k in ('x', 'y', 'width', 'height')) for group in root.iter('g') if 'node' in group.attrib.get('class', '').split() for rect in group.findall('rect')]
        for i, (x, y, w, h) in enumerate(boxes):
            for xx, yy, ww, hh in boxes[i+1:]:
                self.assertFalse(x < xx+ww and xx < x+w and y < yy+hh and yy < y+h, ((x,y,w,h),(xx,yy,ww,hh)))

    def test_chart_rejects_labels_wider_than_node(self):
        data = statechart.definition()
        refs = statechart.router(statechart.SKILL.read_text())
        refs[0]['path'] = 'a' * 30 + '.md'
        with self.assertRaisesRegex(ValueError, 'chart label overflows node'):
            statechart.chart_svg(data, refs, {}, 'current')
        data['entry'][0]['label'] = 'A' * 30
        with self.assertRaisesRegex(ValueError, 'chart label overflows node'):
            statechart.chart_svg(data, [], {}, 'current')

    def test_conditional_paths_connect_correct_nodes(self):
        svg = re.search(r'<svg class="chart".*?</svg>', statechart.render()).group()
        root = ET.fromstring(svg)
        nodes = {g.attrib['data-id']: tuple(float(g.find('rect').attrib[k]) for k in ('x','y','width','height')) for g in root.iter('g') if 'node' in g.attrib.get('class','').split()}
        paths = {(p.attrib.get('data-from'), p.attrib.get('data-to')): p.attrib['d'] for p in root.iter('path') if p.attrib.get('class') == 'edge'}
        def endpoints(path):
            tokens = re.findall(r'[A-Za-z]|-?\d+(?:\.\d+)?', path)
            x = y = None; start = None; i = 0
            while i < len(tokens):
                cmd = tokens[i]; i += 1
                count = {'M':2, 'H':1, 'V':1, 'C':6}[cmd]
                values = list(map(float, tokens[i:i+count])); i += count
                if cmd in ('M','C'): x,y = values[-2:]
                elif cmd == 'H': x = values[0]
                else: y = values[0]
                if start is None: start = (x,y)
            return start, (x,y)
        def on_boundary(point, box):
            x,y,w,h=box; px,py=point
            return x <= px <= x+w and y <= py <= y+h and (px in (x,x+w) or py in (y,y+h))
        for source, target in [('gate','plan_fix'), ('gate','report_rollover'), ('report_rollover','merged')]:
            start,end = endpoints(paths[source,target])
            self.assertTrue(on_boundary(start,nodes[source]), (source,start))
            self.assertTrue(on_boundary(end,nodes[target]), (target,end))

    def test_step_and_reference_controls_are_keyboard_accessible(self):
        svg = re.search(r'<svg class="chart".*?</svg>', statechart.render()).group()
        root = ET.fromstring(svg)
        controls = [g for g in root.iter('g') if g.attrib.get('role') == 'button']
        self.assertTrue(all(g.attrib.get('tabindex') == '0' for g in controls))
        self.assertTrue(any(g.attrib.get('data-owner') == 'plan_fix' and g.attrib.get('data-id') == 'plan-fix.md' for g in controls))
        self.assertIn("e.key==='Enter'||e.key===' '", statechart.render())

    def test_current_and_after_both_have_verdict_badges(self):
        chart = statechart.render()
        views = re.findall(r'<section class="view (?:current|after)"[^>]*>(.*?)</section>', chart)
        self.assertEqual(len(views), 2)
        for view in views:
            self.assertEqual(view.count('class="badge verdict"'), 10)

    def test_chart_reads_labels_from_definition(self):
        source = statechart.ROOT / 'scripts/workflow_definition.json'
        data = json.loads(source.read_text())
        data['gate']['exit_label'] = 'changed {threshold} label'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'workflow_definition.json'
            path.write_text(json.dumps(data))
            chart = statechart.render(definition_path=path)
        self.assertIn('changed 15 label', chart)



    def test_chart_lists_lane_states_and_moves_from_the_lane_table(self):
        page = statechart.render()
        self.assertIn("Parked can move to Planned<", page)
        data = statechart.definition()
        data["lane"]["statuses"][-1]["label"] = "On hold"
        data["lane"]["moves"]["parked"] = ["planned", "running"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow_definition.json"
            path.write_text(json.dumps(data))
            changed = statechart.render(definition_path=path)
        self.assertIn("On hold can move to Planned, Fixing<", changed)
        self.assertNotIn("Parked can move to Planned<", changed)

if __name__ == '__main__':
    unittest.main()
