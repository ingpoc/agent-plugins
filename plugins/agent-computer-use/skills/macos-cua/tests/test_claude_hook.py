"""Claude Code hook event payloads map to exact island-post command arguments."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3]
HOOK = PLUGIN / "bin/acu-claude-hook"
BASE = ["--source", "claude", "--id", "claude-code:s1", "--app", "Claude"]


class ClaudeHookTests(unittest.TestCase):
    def setup_home(self, home):
        bin_dir = Path(home) / "plugin/bin"
        bin_dir.mkdir(parents=True)
        hook_copy = bin_dir / "acu-claude-hook"
        hook_copy.write_bytes(HOOK.read_bytes())
        hook_copy.chmod(0o755)
        log = Path(home) / "posts.jsonl"
        fake = bin_dir / "island-post"
        fake.write_text(
            f"#!/usr/bin/env python3\nimport json,sys\n"
            f"open({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        return log

    def run_hook(self, home, payload, args=(), stdin=None):
        proc = subprocess.run(
            [str(Path(home) / "plugin/bin/acu-claude-hook"), *args],
            input=json.dumps(payload) if stdin is None else stdin,
            text=True, capture_output=True, env=os.environ | {"HOME": home}, timeout=8,
        )
        log = Path(home) / "posts.jsonl"
        posts = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return proc, posts

    def assert_post(self, actual, *expected, session_id="s1"):
        base = ["--source", "claude", "--id", f"claude-code:{session_id}", "--app", "Claude"]
        self.assertEqual(actual, base + list(expected))

    def test_session_start_caches_only_allowlisted_metadata_without_post(self):
        with tempfile.TemporaryDirectory() as home:
            log = self.setup_home(home)
            proc, posts = self.run_hook(home, {
                "hook_event_name": "SessionStart", "session_id": "s1",
                "model": "claude-sonnet-5-5", "session_title": "Build app",
                "prompt": "SECRET", "transcript_path": "SECRET",
            })
            cache = Path(home) / ".cache/macos-cua/claude-hook/s1.json"
            self.assertEqual((proc.returncode, proc.stdout, posts), (0, "", []))
            self.assertEqual(json.loads(cache.read_text()), {"model": "Sonnet 5.5", "session_title": "Build app"})
            self.assertNotIn("SECRET", cache.read_text())

    def test_prompt_uses_payload_title_then_cached_title_then_cwd(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.run_hook(home, {"hook_event_name": "SessionStart", "session_id": "s1",
                                 "model": "claude-opus-5", "session_title": "Cached"})
            _, posts = self.run_hook(home, {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                            "cwd": "/tmp/project", "session_title": "Payload title",
                                            "effort": {"level": "high"}, "prompt": "SECRET",
                                            "transcript_path": "SECRET"})
            self.assert_post(posts[-1], "--kind", "running", "--title", "Payload title", "--ttl", "1800",
                             "--meta", "Opus 5 · high")
            _, posts = self.run_hook(home, {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                            "cwd": "/tmp/project", "effort": {"level": "low"}})
            self.assert_post(posts[-1], "--kind", "running", "--title", "Payload title", "--ttl", "1800",
                             "--meta", "Opus 5 · low")
            (Path(home) / ".cache/macos-cua/claude-hook/s1.json").unlink()
            _, posts = self.run_hook(home, {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                            "cwd": "/tmp/project"})
            self.assert_post(posts[-1], "--kind", "running", "--title", "Claude Code · project", "--ttl", "1800")
            self.assertNotIn("SECRET", (Path(home) / "posts.jsonl").read_text())

    def test_prompt_metadata_updates_cache_for_later_events_and_resume_preserves_it(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.run_hook(home, {"hook_event_name": "SessionStart", "session_id": "s1",
                                 "model": "claude-sonnet-5-5", "session_title": "Original"})
            self.run_hook(home, {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                 "cwd": "/work/fallback", "session_title": "Current title",
                                 "model": "claude-opus-5"})
            cache = Path(home) / ".cache/macos-cua/claude-hook/s1.json"
            self.assertEqual(json.loads(cache.read_text()), {"session_title": "Current title", "model": "Opus 5"})
            # Resume may omit metadata; SessionStart retains the last prompt metadata.
            self.run_hook(home, {"hook_event_name": "SessionStart", "session_id": "s1"})
            self.assertEqual(json.loads(cache.read_text()), {"session_title": "Current title", "model": "Opus 5"})
            _, posts = self.run_hook(home, {"hook_event_name": "Notification", "session_id": "s1",
                                            "notification_type": "permission_prompt"})
            self.assert_post(posts[-1], "--kind", "needs_you", "--title", "Current title",
                             "--detail", "Permission prompt")
            _, posts = self.run_hook(home, {"hook_event_name": "Stop", "session_id": "s1"})
            self.assert_post(posts[-1], "--kind", "done", "--title", "Current title")

    def test_cwd_fallback_title_is_cached_for_notifications_and_stop(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            _, posts = self.run_hook(home, {"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                            "cwd": "/work/project"})
            self.assert_post(posts[-1], "--kind", "running", "--title", "Claude Code · project", "--ttl", "1800")
            _, posts = self.run_hook(home, {"hook_event_name": "PermissionRequest", "session_id": "s1"})
            self.assert_post(posts[-1], "--kind", "needs_you", "--title", "Claude Code · project",
                             "--detail", "Permission requested")
            _, posts = self.run_hook(home, {"hook_event_name": "Stop", "session_id": "s1"})
            self.assert_post(posts[-1], "--kind", "done", "--title", "Claude Code · project")

    def test_known_notification_types_post_exact_needs_you_args(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.run_hook(home, {"hook_event_name": "SessionStart", "session_id": "s1", "session_title": "Cached"})
            for notification, detail in (
                ("permission_prompt", "Permission prompt"),
                ("elicitation_dialog", "Elicitation dialog"),
                ("elicitation_url_dialog", "Elicitation url dialog"),
                ("agent_needs_input", "Agent needs input"),
            ):
                _, posts = self.run_hook(home, {"hook_event_name": "Notification", "session_id": "s1",
                                                "notification_type": notification})
                self.assert_post(posts[-1], "--kind", "needs_you", "--title", "Cached", "--detail", detail)

    def test_ignored_notification_types_do_not_post(self):
        with tempfile.TemporaryDirectory() as home:
            log = self.setup_home(home)
            for notification in ("idle_prompt", "auth_success", "elicitation_complete", "elicitation_response"):
                _, posts = self.run_hook(home, {"hook_event_name": "Notification", "session_id": "s1",
                                                "notification_type": notification})
                self.assertEqual(posts, [])
            self.assertFalse(log.exists())

    def test_permission_request_posts_exact_attention_args(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.run_hook(home, {"hook_event_name": "SessionStart", "session_id": "s1", "session_title": "Review"})
            _, posts = self.run_hook(home, {"hook_event_name": "PermissionRequest", "session_id": "s1"})
            self.assert_post(posts[-1], "--kind", "needs_you", "--title", "Review", "--detail", "Permission requested")

    def test_stop_posts_exact_done_args(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.run_hook(home, {"hook_event_name": "SessionStart", "session_id": "s1", "session_title": "Review"})
            _, posts = self.run_hook(home, {"hook_event_name": "Stop", "session_id": "s1"})
            self.assert_post(posts[-1], "--kind", "done", "--title", "Review")

    def test_stop_failure_error_humanization_uses_detail(self):
        cases = {
            "rate_limit": "Rate limited", "overloaded": "Overloaded",
            "authentication_failed": "Sign-in failed", "billing_error": "Billing",
            "api_unavailable": "Api Unavailable",
        }
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            for error, detail in cases.items():
                _, posts = self.run_hook(home, {"hook_event_name": "StopFailure", "session_id": "s1", "error": error})
                self.assert_post(posts[-1], "--kind", "error", "--title", "Claude Code", "--detail", detail)

    def test_session_end_posts_clear_then_removes_cache(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.run_hook(home, {"hook_event_name": "SessionStart", "session_id": "s1", "session_title": "Review"})
            cache = Path(home) / ".cache/macos-cua/claude-hook/s1.json"
            _, posts = self.run_hook(home, {"hook_event_name": "SessionEnd", "session_id": "s1"})
            self.assert_post(posts[-1], "--kind", "clear")
            self.assertFalse(cache.exists())

    def test_model_shortening_unknown_model_and_initiator(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            for index, (slug, expected) in enumerate((("claude-opus-5", "Opus 5"), ("claude-sonnet-5-5", "Sonnet 5.5"),
                                   ("claude-haiku-4", "Haiku 4"), ("unknown-model", None))):
                session_id = f"model-{index}"
                _, posts = self.run_hook(home, {"hook_event_name": "UserPromptSubmit", "session_id": session_id,
                                                "cwd": "/work/project", "model": slug, "effort": {"level": "high"}},
                                         ["--initiator", "acu_exec.grokbot.cursor"])
                args = ["--kind", "running", "--title", "Claude Code · project", "--ttl", "1800"]
                if expected:
                    args.extend(("--meta", f"{expected} · high"))
                else:
                    args.extend(("--meta", "high"))
                args.extend(("--initiator", "acu_exec.grokbot.cursor"))
                self.assert_post(posts[-1], *args, session_id=session_id)

    def test_cache_corruption_and_malformed_input_fail_open_without_stdout(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            cache = Path(home) / ".cache/macos-cua/claude-hook/broken.json"
            cache.parent.mkdir(parents=True)
            cache.write_text("{")
            proc, posts = self.run_hook(home, {"hook_event_name": "UserPromptSubmit", "session_id": "broken",
                                               "cwd": "/tmp/fallback"})
            self.assertEqual((proc.returncode, proc.stdout), (0, ""))
            self.assert_post(posts[-1], "--kind", "running", "--title", "Claude Code · fallback", "--ttl", "1800", session_id="broken")
            count = len(posts)
            proc, posts = self.run_hook(home, {}, stdin="not json")
            self.assertEqual((proc.returncode, proc.stdout, len(posts)), (0, "", count))

    def test_post_timeout_fails_open_without_stdout(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            fake = Path(home) / "plugin/bin/island-post"
            fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(4)\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            proc, posts = self.run_hook(home, {"hook_event_name": "Stop", "session_id": "s1"})
            self.assertEqual((proc.returncode, proc.stdout, posts), (0, "", []))

    def test_invalid_settings_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            settings = Path(home) / ".claude/settings.json"
            settings.parent.mkdir()
            settings.write_text("{invalid")
            result, _ = self.run_hook(home, {}, ["--install"])
            self.assertEqual(settings.read_text(), "{invalid")
            self.assertEqual(result.returncode, 1, "setup reports failure; only hook events fail open")
            settings.write_text(json.dumps({"hooks": {"Stop": {"not": "a list"}}}))
            result, _ = self.run_hook(home, {}, ["--install"])
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(settings.read_text()), {"hooks": {"Stop": {"not": "a list"}}})

    def test_install_uninstall_is_idempotent_and_preserves_foreign_mixed_group(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            settings = Path(home) / ".claude/settings.json"
            settings.parent.mkdir()
            initial = {"other": 7, "hooks": {"Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "foreign"},
                {"type": "command", "command": "/old/acu-claude-hook"},
            ]}]}}
            settings.write_text(json.dumps(initial))
            self.run_hook(home, {}, ["--install"])
            installed = json.loads(settings.read_text())
            self.run_hook(home, {}, ["--install"])
            self.assertEqual(installed, json.loads(settings.read_text()))
            self.assertTrue(Path(str(settings) + ".acu-bak").exists())
            self.assertEqual(installed["other"], 7)
            self.assertEqual(installed["hooks"]["Stop"][0]["hooks"][0]["command"], "foreign")
            all_hooks = [entry for groups in installed["hooks"].values() for group in groups
                         for entry in group.get("hooks", [])]
            ours = [entry for entry in all_hooks if "acu-claude-hook" in entry.get("command", "")]
            self.assertEqual(len(ours), 7)
            self.assertTrue(all(entry["timeout"] == 5 for entry in ours))
            self.run_hook(home, {}, ["--uninstall"])
            final = json.loads(settings.read_text())
            self.assertEqual(final, {"other": 7, "hooks": {"Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "foreign"},
            ]}]}})

    def test_uninstall_preserves_unrelated_empty_and_named_hook_groups(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            path = Path(home) / ".claude/settings.json"
            path.parent.mkdir()
            foreign = {"hooks": {"Stop": [
                {"matcher": "foreign", "hooks": []},
                {"hooks": [{"type": "command", "command": "echo acu-claude-hook"}]},
            ]}}
            path.write_text(json.dumps(foreign))
            self.run_hook(home, {}, ["--uninstall"])
            self.assertEqual(json.loads(path.read_text()), foreign)
            self.run_hook(home, {}, ["--install"])
            self.run_hook(home, {}, ["--uninstall"])
            self.assertEqual(json.loads(path.read_text()), foreign)

    def prepare_plugin_files(self, home):
        root = Path(home) / "plugin"
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "agent-computer-use"}))
        (root / "hooks").mkdir()
        (root / "hooks/hooks.json").write_text("{}")
        return root

    def test_install_registers_from_uninstalled_plugin_checkout(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.prepare_plugin_files(home)
            self.run_hook(home, {}, ["--install"])
            settings = json.loads((Path(home) / ".claude/settings.json").read_text())
            ours = [entry for groups in settings["hooks"].values() for group in groups
                    for entry in group.get("hooks", []) if "acu-claude-hook" in entry.get("command", "")]
            self.assertEqual(len(ours), 7)

    def test_install_skips_registration_for_enabled_installed_plugin_hooks(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            root = self.prepare_plugin_files(home)
            plugin_id = "agent-computer-use@test-marketplace"
            claude_dir = Path(home) / ".claude"
            claude_dir.mkdir(exist_ok=True)
            (claude_dir / "settings.json").write_text(json.dumps({"enabledPlugins": {plugin_id: True}}))
            plugins_dir = claude_dir / "plugins"
            plugins_dir.mkdir()
            (plugins_dir / "installed_plugins.json").write_text(json.dumps({
                "plugins": {plugin_id: [{"installPath": str(root)}]},
            }))
            self.run_hook(home, {}, ["--install"])
            settings = json.loads((claude_dir / "settings.json").read_text())
            self.assertEqual(settings, {"enabledPlugins": {plugin_id: True}})

    def test_install_skips_checkout_fallback_when_enabled_cache_has_plugin_hooks(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            self.prepare_plugin_files(home)  # The hook is running from this checkout.
            installed_root = Path(home) / "plugin-cache/agent-computer-use"
            installed_root.mkdir(parents=True)
            (installed_root / ".claude-plugin").mkdir()
            (installed_root / ".claude-plugin/plugin.json").write_text(
                json.dumps({"name": "agent-computer-use"})
            )
            (installed_root / "hooks").mkdir()
            (installed_root / "hooks/hooks.json").write_text("{}")
            plugin_id = "agent-computer-use@test-marketplace"
            claude_dir = Path(home) / ".claude"
            claude_dir.mkdir(exist_ok=True)
            (claude_dir / "settings.json").write_text(json.dumps({"enabledPlugins": {plugin_id: True}}))
            plugins_dir = claude_dir / "plugins"
            plugins_dir.mkdir()
            (plugins_dir / "installed_plugins.json").write_text(json.dumps({
                "plugins": {plugin_id: [{"installPath": str(installed_root)}]},
            }))
            self.run_hook(home, {}, ["--install"])
            self.assertEqual(
                json.loads((claude_dir / "settings.json").read_text()),
                {"enabledPlugins": {plugin_id: True}},
            )

    def test_invalid_cli_always_exits_zero_without_stdout(self):
        with tempfile.TemporaryDirectory() as home:
            self.setup_home(home)
            for args in [["--unknown"], ["--help"], ["--initiator"]]:
                result, _ = self.run_hook(home, {}, args)
                self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
