"""bin/acu-cursor-hook: Cursor hook payloads -> island-post calls (read-only on Cursor's DB)."""
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3]
HOOK = PLUGIN / "bin/acu-cursor-hook"


class CursorHookTests(unittest.TestCase):
    def run_hook(self, payload, home):
        bin_dir = Path(home) / "plugin/bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "acu-cursor-hook").write_bytes(HOOK.read_bytes())
        (bin_dir / "acu-cursor-hook").chmod(0o755)
        log = Path(home) / "posts.jsonl"
        fake = bin_dir / "island-post"
        fake.write_text(f"#!/usr/bin/env python3\nimport json,sys\nopen({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        out = subprocess.run([str(bin_dir / "acu-cursor-hook")], input=json.dumps(payload), text=True,
                             capture_output=True, env=os.environ | {"HOME": home}, timeout=10)
        posts = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return json.loads(out.stdout), posts

    def state_db(self, home, conv, name, model_config=None):
        db = Path(home) / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{conv}", json.dumps({"name": name, **({"modelConfig": model_config} if model_config else {})})))
        con.commit(); con.close()

    def test_prompt_posts_working_row_with_chat_name_and_model_meta(self):
        with tempfile.TemporaryDirectory() as home:
            self.state_db(home, "c1", "General chat")
            reply, posts = self.run_hook({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c1",
                                          "prompt": "how is it going", "model": "grok-4.7-high-fast",
                                          "model_id": "grok-4.7",
                                          "model_params": [{"id": "effort", "value": "high"}]}, home)
            self.assertEqual(reply, {"continue": True})
            args = posts[0]
            self.assertEqual(args[args.index("--kind") + 1], "running")
            self.assertEqual(args[args.index("--app") + 1], "Cursor")
            self.assertEqual(args[args.index("--title") + 1], "General chat")
            self.assertEqual(args[args.index("--meta") + 1], "Grok 4.7 · high")
            self.assertEqual(args[args.index("--source") + 1], "cursor")

    def test_fast_from_payload_or_chat_config_and_done_keeps_meta(self):
        config = {"modelName": "grok-4.7", "selectedModels": [{"modelId": "grok-4.7", "parameters": [
            {"id": "context", "value": "256k"}, {"id": "reasoning_effort", "value": "high"}, {"id": "fast", "value": "true"}]}]}
        with tempfile.TemporaryDirectory() as home:
            self.state_db(home, "c4", "General chat", config)
            _, posts = self.run_hook({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c4", "prompt": "hi",
                                      "model_id": "grok-4.7", "model_params": [{"id": "reasoning_effort", "value": "high"}]}, home)
            self.assertEqual(posts[0][posts[0].index("--meta") + 1], "Grok 4.7 · high · fast")
            _, posts = self.run_hook({"hook_event_name": "stop", "conversation_id": "c4", "status": "completed"}, home)
            self.assertEqual(posts[-1][posts[-1].index("--kind") + 1], "done")
            self.assertEqual(posts[-1][posts[-1].index("--meta") + 1], "Grok 4.7 · high · fast")
        with tempfile.TemporaryDirectory() as home:
            self.state_db(home, "c5", "Other", config)
            _, posts = self.run_hook({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c5", "prompt": "x"}, home)
            self.assertEqual(posts[0][posts[0].index("--meta") + 1], "Grok 4.7 · high · fast", "no payload model: chat config")
        with tempfile.TemporaryDirectory() as home:
            self.state_db(home, "c6", "Other", config)
            _, posts = self.run_hook({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c6", "prompt": "x",
                                      "model_id": "claude-4.5-sonnet"}, home)
            self.assertEqual(posts[0][posts[0].index("--meta") + 1], "Sonnet 4.5", "another model never borrows params")

    def test_unnamed_chat_uses_trimmed_prompt_and_stop_marks_done(self):
        with tempfile.TemporaryDirectory() as home:
            self.run_hook({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c2",
                           "prompt": "Refactor the island roster: split rows into a view", "model": "default"}, home)
            reply, posts = self.run_hook({"hook_event_name": "stop", "conversation_id": "c2", "status": "completed"}, home)
            self.assertEqual(reply, {})
            self.assertEqual(posts[0][posts[0].index("--title") + 1], "Refactor the island roster")
            self.assertNotIn("--meta", posts[0])
            self.assertEqual(posts[1][posts[1].index("--kind") + 1], "done")
            self.assertEqual(posts[1][posts[1].index("--title") + 1], "Refactor the island roster")

    def test_aborted_turn_and_session_end_clear_the_row(self):
        with tempfile.TemporaryDirectory() as home:
            _, posts = self.run_hook({"hook_event_name": "stop", "conversation_id": "c3", "status": "aborted"}, home)
            self.assertEqual(posts[0][posts[0].index("--kind") + 1], "clear")
            _, posts = self.run_hook({"hook_event_name": "sessionEnd", "conversation_id": "c3"}, home)
            self.assertEqual(posts[-1][posts[-1].index("--kind") + 1], "clear")

    def test_bad_input_fails_open(self):
        with tempfile.TemporaryDirectory() as home:
            bin_dir = Path(home) / "plugin/bin"; bin_dir.mkdir(parents=True)
            out = subprocess.run([str(HOOK)], input="not json", text=True, capture_output=True,
                                 env=os.environ | {"HOME": home}, timeout=10)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(json.loads(out.stdout), {})


if __name__ == "__main__":
    unittest.main()
