"""The optional ledger connection fails closed and never starts a different store."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from voice_cua.ledger_transport import LedgerTransport, LedgerUnavailable


class LedgerTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.package = root / 'plugin'
        self.data = root / 'data'
        (self.package / 'bin').mkdir(parents=True)
        self.data.mkdir()
        (self.package / 'plugin.json').write_text(json.dumps({'name': 'context-ledger'}))
        (self.data / 'binding.json').write_text('{}')
        self.launcher = self.package / 'bin/context-ledger-mcp'
        self.launcher.write_text('''#!/usr/bin/python3
import sys,json
for line in sys.stdin:
 req=json.loads(line)
 if "id" not in req: continue
 result={} if req["method"]=="initialize" else {"content":[{"type":"text","text":json.dumps({"ok":True})}]}
 print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":result}),flush=True)
''')
        self.launcher.chmod(0o700)
        self.transport = LedgerTransport(self.package, self.data)
        self.addCleanup(self.transport.close)

    def test_scoped_calls_use_one_existing_server(self):
        self.assertEqual(self.transport.call('find', {'query': 'decision'}), {'ok': True})
        first = self.transport._process
        self.assertEqual(self.transport.call('find', {'query': 'other'}), {'ok': True})
        self.assertIs(first, self.transport._process)

    def test_missing_binding_fails_closed(self):
        (self.data / 'binding.json').unlink()
        with self.assertRaises(LedgerUnavailable):
            self.transport.call('find', {})
        self.assertIsNone(self.transport._process)

    def test_cannot_call_unrestricted_tools(self):
        with self.assertRaises(ValueError):
            self.transport.call('get', {'decision_id': 'arbitrary'})
        self.assertIsNone(self.transport._process)

    def test_rejects_wrong_package(self):
        (self.package / 'plugin.json').write_text('{"name":"other"}')
        with self.assertRaises(LedgerUnavailable):
            self.transport.call('find', {})

if __name__ == '__main__':
    unittest.main()
