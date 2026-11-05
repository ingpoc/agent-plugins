"""developer-observability posts plugin activity to the ACU island (0.2.87): mark outside right, nothing inside."""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / 'runtime'
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

import island  # noqa: E402


class FakeActivity:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    @staticmethod
    def normalize_agent(name: str) -> str:
        return name.strip() or 'agent'

    def call_started(self, plugin, agent, key, in_flight_s=120.0):
        if self.fail:
            raise OSError('island down')
        self.calls.append(('start', plugin, agent, key))

    def call_finished(self, plugin, agent, key):
        self.calls.append(('finish', plugin, agent, key))


class IslandTests(unittest.TestCase):
    def setUp(self):
        island.FAILURES['count'] = 0

    def test_each_call_holds_the_mark_and_drops_it(self):
        fake = FakeActivity()
        with mock.patch.object(island, '_ACTIVITY', fake), mock.patch.dict('os.environ', {'DEVELOPER_OBSERVABILITY_ISLAND': '1'}):
            @island.tracked(lambda: 'Codex')
            def observability_issues(project_id: str, limit: int = 20):
                return {'project': project_id, 'limit': limit}

            self.assertEqual(observability_issues('acu', limit=5), {'project': 'acu', 'limit': 5})
        self.assertEqual(fake.calls, [('start', 'developer-observability', 'Codex', 'Codex'),
                                      ('finish', 'developer-observability', 'Codex', 'Codex')])

    def test_signature_is_kept_for_the_mcp_schema(self):
        def tool(project_id: str, status: str = 'all') -> dict:
            return {}

        wrapped = island.tracked(lambda: 'agent')(tool)
        self.assertEqual(list(inspect.signature(wrapped).parameters), ['project_id', 'status'])
        self.assertEqual(wrapped.__name__, 'tool')

    def test_island_failures_never_break_a_call_and_errors_still_drop_the_mark(self):
        with mock.patch.object(island, '_ACTIVITY', FakeActivity(fail=True)), mock.patch.dict('os.environ', {'DEVELOPER_OBSERVABILITY_ISLAND': '1'}):
            self.assertEqual(island.tracked(lambda: 'Cursor')(lambda: 7)(), 7)
        self.assertEqual(island.FAILURES['count'], 1)
        fake = FakeActivity()
        with mock.patch.object(island, '_ACTIVITY', fake), mock.patch.dict('os.environ', {'DEVELOPER_OBSERVABILITY_ISLAND': '1'}):
            def boom():
                raise ValueError('collector_failed')
            with self.assertRaises(ValueError):
                island.tracked(lambda: 'Codex')(boom)()
        self.assertEqual([c[0] for c in fake.calls], ['start', 'finish'])

    def test_disabled_posts_nothing(self):
        fake = FakeActivity()
        with mock.patch.object(island, '_ACTIVITY', fake), mock.patch.dict('os.environ', {'DEVELOPER_OBSERVABILITY_ISLAND': 'off'}):
            island.tracked(lambda: 'Codex')(lambda: None)()
        self.assertEqual(fake.calls, [])

    def test_acu_island_activity_knows_the_plugin(self):
        path = Path(__file__).resolve().parents[2] / 'agent-computer-use/bin/island_activity.py'
        if not path.is_file():
            self.skipTest('agent-computer-use not beside this plugin')
        with mock.patch.dict('os.environ', {'DEVELOPER_OBSERVABILITY_ISLAND_ACTIVITY': str(path)}), \
                mock.patch.object(island, '_ACTIVITY', island._UNSET):
            module = island.activity()
        self.assertIn('developer-observability', module.MARKS)
        row = module.row('developer-observability', 'Codex', 'Codex', 10)
        self.assertEqual(row['plugin'], 'developer-observability')
        self.assertEqual(row['title'], 'Using Developer Observability')
        self.assertNotIn('target_app', row, 'dev-obs adds nothing inside the pill')
        island._ACTIVITY = island._UNSET


if __name__ == '__main__':
    unittest.main()
