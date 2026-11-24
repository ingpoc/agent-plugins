"""bin/acu-grok-hook: documented Grok SubagentStart/SubagentStop events -> island-post."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN = next(parent for parent in Path(__file__).resolve().parents if (parent / "plugin.json").is_file())
HOOK = PLUGIN / "bin/acu-grok-hook"


class GrokHookTests(unittest.TestCase):
    def run_hook(self, payload, home, env=None):
        bin_dir = Path(home) / "plugin/bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "acu-grok-hook").write_bytes(HOOK.read_bytes())
        (bin_dir / "acu-grok-hook").chmod(0o755)
        log = Path(home) / "posts.jsonl"
        fake = bin_dir / "island-post"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json,sys\nopen(%r,'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
            % str(log)
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        merged = os.environ | {"HOME": home}
        if env:
            merged.update(env)
        out = subprocess.run([str(bin_dir / "acu-grok-hook")], input=json.dumps(payload), text=True,
                             capture_output=True, env=merged, timeout=10)
        posts = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return out.returncode, posts

    def test_subagent_start_posts_a_running_grok_bot_row(self):
        with tempfile.TemporaryDirectory() as home:
            code, posts = self.run_hook({
                "hookEventName": "SubagentStart",
                "sessionId": "sess-1",
                "subagentId": "child-9",
                "title": "Collect coverage gaps",
            }, home)
            self.assertEqual(code, 0)
            args = posts[0]
            self.assertEqual(args[args.index("--source") + 1], "grok-bot")
            self.assertEqual(args[args.index("--kind") + 1], "running")
            self.assertEqual(args[args.index("--app") + 1], "Grok Bot")
            self.assertEqual(args[args.index("--title") + 1], "Collect coverage gaps")
            self.assertTrue(args[args.index("--id") + 1].startswith("subagent-"))
            self.assertIn("sess-1", args[args.index("--id") + 1])

    def test_subagent_stop_success_posts_done(self):
        with tempfile.TemporaryDirectory() as home:
            code, posts = self.run_hook({
                "hook_event_name": "SubagentStop",
                "session_id": "sess-1",
                "subagent_id": "child-9",
                "title": "Collect coverage gaps",
                "status": "completed",
            }, home)
            self.assertEqual(code, 0)
            args = posts[0]
            self.assertEqual(args[args.index("--kind") + 1], "done")
            self.assertNotIn("--detail", args)

    def test_subagent_stop_with_reason_posts_stopped(self):
        with tempfile.TemporaryDirectory() as home:
            code, posts = self.run_hook({
                "hookEventName": "SubagentStop",
                "sessionId": "sess-1",
                "subagentId": "child-9",
                "title": "Collect coverage gaps",
                "status": "cancelled",
                "reason": "Owner stopped the lane",
            }, home)
            self.assertEqual(code, 0)
            args = posts[0]
            self.assertEqual(args[args.index("--kind") + 1], "stopped")
            self.assertEqual(args[args.index("--detail") + 1], "Owner stopped the lane")

    def test_env_event_name_is_enough_and_hook_fails_open(self):
        with tempfile.TemporaryDirectory() as home:
            code, posts = self.run_hook(
                {"title": "Plan review"},
                home,
                env={"GROK_HOOK_EVENT": "SubagentStart", "GROK_SESSION_ID": "from-env"},
            )
            self.assertEqual(code, 0)
            args = posts[0]
            self.assertEqual(args[args.index("--kind") + 1], "running")
            self.assertIn("from-env", args[args.index("--id") + 1])

    def test_malformed_stdin_exits_zero_and_posts_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            bin_dir = Path(home) / "plugin/bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            hook = bin_dir / "acu-grok-hook"
            hook.write_bytes(HOOK.read_bytes())
            hook.chmod(0o755)
            log = Path(home) / "posts.jsonl"
            fake = bin_dir / "island-post"
            fake.write_text(
                "#!/usr/bin/env python3\nimport json,sys\nopen(%r,'a').write('x')\n" % str(log)
            )
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            out = subprocess.run([str(hook)], input="not-json", text=True,
                                 capture_output=True, env=os.environ | {"HOME": home}, timeout=10)
            self.assertEqual(out.returncode, 0)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
