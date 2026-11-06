import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/context-ledger'))
sys.path.insert(0, str(ROOT / 'plugins/agent-computer-use/runtime/voice-cua/python'))
from context_ledger.store import Store, init_ledger
from voice_cua.ledger_helper import LedgerHelper


class SamanthaLedgerIntegrationTests(unittest.TestCase):
    def test_recall_candidates_stay_within_the_requested_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'ledger'
            data.mkdir()
            init_ledger(data, None, 'test', 'tester')
            store = Store(data, actor_channel='agent')
            store.ab_today = lambda: '2026-10-17'
            for site, value in [('example', 'A'), ('other', 'B')]:
                store.save({'request_id': str(uuid.uuid4()), 'type': 'user_decision',
                            'scope': 'tool:' + site, 'rule': 'profile_selection',
                            'decision': value, 'why': 'Owner preference', 'entities': [site]}, mcp=True)
            transport = Mock()
            transport.call.side_effect = lambda name, args: {'ok': True, 'data': store.find(args, mcp=True)}
            helper = LedgerHelper(transport, Mock(), Path(tmp) / 'pending')
            result = helper.run(operation='recall', scope='tool:example', rule='profile', need='profile')
            self.assertEqual(result['status'], 'needs_disambiguation')
            self.assertEqual([m['decision'] for m in result['data']['matches']], ['A'])
            store.close()
