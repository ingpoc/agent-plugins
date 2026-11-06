import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ledger_loop import Fail, Mcp


class LedgerHttpErrorTests(unittest.TestCase):
    def test_reset_while_reading_unauthorized_body_still_reports_unauthorized(self):
        client = Mcp('http://127.0.0.1/mcp', 'test', 'test-task')
        error = urllib.error.HTTPError(client.url, 401, 'Unauthorized', {}, io.BytesIO())
        error.read = Mock(side_effect=ConnectionResetError('reset'))
        client.opener.open = Mock(side_effect=error)
        with self.assertRaisesRegex(Fail, 'UNAUTHORIZED'):
            client._post({'id': 1})
