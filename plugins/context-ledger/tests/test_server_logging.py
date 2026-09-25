#!/usr/bin/env python3
"""Ensure generic MCP errors are diagnosable without logging payload text."""
import json
import logging
import unittest

from context_ledger.server import _call


class InternalErrorLoggingTests(unittest.TestCase):
    def test_logs_sanitized_location_but_hides_exception_message(self):
        def fail(_arguments):
            raise RuntimeError("sensitive-ledger-payload")

        self.assertEqual(logging.getLogger().level, logging.CRITICAL)
        with self.assertLogs("context_ledger.server", level="ERROR") as logs:
            result = _call(fail, {})

        payload = json.loads(result.content[0].text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INTERNAL")
        rendered = "\n".join(logs.output)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("server.py:", rendered)
        self.assertNotIn("sensitive-ledger-payload", rendered)


if __name__ == "__main__":
    unittest.main()
