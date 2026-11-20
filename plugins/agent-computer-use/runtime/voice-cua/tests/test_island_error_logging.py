"""ACU-18: an island error posted to the service is logged with its raw reason."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from voice_cua import island_facade  # noqa: E402


class RemoteIslandErrorLoggingTests(unittest.TestCase):
    def test_remote_error_card_logs_raw_reason(self):
        with patch.object(island_facade, "use_remote", return_value=True), \
                patch.object(island_facade, "_post") as post, \
                patch("voice_cua.activity_log.log_event") as log:
            island_facade.island_publish("error", title="Voice model unavailable",
                                         detail="setup_error: Voice model unavailable: acu01-unknown-model")
        post.assert_called_once()
        rows = [c for c in log.call_args_list if c.args and c.args[0] == "island_error_raw"]
        self.assertEqual(len(rows), 1)
        self.assertIn("acu01-unknown-model", rows[0].kwargs["raw"])
        self.assertTrue(rows[0].kwargs["shown"])

    def test_remote_non_error_is_not_logged_as_error(self):
        with patch.object(island_facade, "use_remote", return_value=True), \
                patch.object(island_facade, "_post"), \
                patch("voice_cua.activity_log.log_event") as log:
            island_facade.island_publish("listening", title="Listening")
        self.assertFalse([c for c in log.call_args_list if c.args and c.args[0] == "island_error_raw"])


if __name__ == "__main__":
    unittest.main()
