#!/usr/bin/env python3
"""Fail-loud checks for the marketplace remove-then-add script. No live CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "refresh_cursor_marketplace_index.sh"

FAKE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    from pathlib import Path

    root = Path(os.environ["FAKE_ROOT"])
    log = root / "argv.log"
    args = sys.argv[1:]
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\\n")
    if "--git-ref" in args or "update" in args:
        print("refused", file=sys.stderr)
        raise SystemExit(2)
    cmd = args[2] if len(args) > 2 else ""
    state_path = root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if cmd == "list":
        json.dump(state["marketplaces"], sys.stdout)
        print()
        raise SystemExit(0)
    if cmd == "remove":
        target = args[-1]
        state["marketplaces"] = [
            item for item in state["marketplaces"] if item.get("name") != target
        ]
        state_path.write_text(json.dumps(state))
        raise SystemExit(0)
    if cmd == "add":
        adds = state.get("adds", 0) + 1
        state["adds"] = adds
        mode = state["mode"]
        fail = mode == "fail_add" or (mode == "fail_once" and adds == 1)
        if fail:
            state_path.write_text(json.dumps(state))
            print("add failed", file=sys.stderr)
            raise SystemExit(1)
        sha = state["new_sha"]
        version = state.get("indexed_version", state["good_version"])
        index = Path(os.environ["INDEX_ROOT"]) / sha / "plugins"
        if mode == "zero_plugins":
            index.mkdir(parents=True, exist_ok=True)
        else:
            ledger = index / "context-ledger"
            ledger.mkdir(parents=True, exist_ok=True)
            (ledger / "plugin.json").write_text(
                json.dumps({"name": "context-ledger", "version": version}),
                encoding="utf-8",
            )
            other = index / "comet-control"
            other.mkdir(parents=True, exist_ok=True)
            (other / "plugin.json").write_text(
                json.dumps({"name": "comet-control", "version": "1.0.0"}),
                encoding="utf-8",
            )
        state["marketplaces"] = [{
            "name": "ingpoc-agent-plugins",
            "gitUrl": "https://github.com/ingpoc/agent-plugins",
            "gitRef": sha,
            "scope": "user",
        }]
        state_path.write_text(json.dumps(state))
        raise SystemExit(0)
    print("unknown command", file=sys.stderr)
    raise SystemExit(2)
    """
)


class RefreshMarketplaceIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.index = self.root / "index"
        self.repo.mkdir()
        self.index.mkdir()
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("GIT_"):
                del env[key]
        env.update({
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        })
        self.git_env = env
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True, env=env)
        ledger = self.repo / "plugins" / "context-ledger"
        ledger.mkdir(parents=True)
        (ledger / "plugin.json").write_text(
            json.dumps({"name": "context-ledger", "version": "0.1.19"}),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "plugins"], cwd=self.repo, check=True, env=env)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "version"],
            cwd=self.repo,
            check=True,
            env=env,
        )
        self.sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, env=env
        ).strip()
        self.old = "638b79f1b3d140febde1b0f25b2c684bcb45b707"
        fake = self.root / "fake-agent.py"
        fake.write_text(FAKE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.fake = fake

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_state(self, marketplaces: list[dict], mode: str, **extra: object) -> None:
        state = {
            "marketplaces": marketplaces,
            "mode": mode,
            "new_sha": self.sha,
            "good_version": "0.1.19",
            "adds": 0,
        }
        state.update(extra)
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def registration(self, git_ref: str) -> dict:
        return {
            "name": "ingpoc-agent-plugins",
            "gitUrl": "https://github.com/ingpoc/agent-plugins",
            "gitRef": git_ref,
            "scope": "user",
        }

    def seed_index(self, git_ref: str, version: str = "0.1.19") -> None:
        ledger = self.index / git_ref / "plugins" / "context-ledger"
        ledger.mkdir(parents=True)
        (ledger / "plugin.json").write_text(
            json.dumps({"name": "context-ledger", "version": version}),
            encoding="utf-8",
        )

    def run_script(self) -> subprocess.CompletedProcess[str]:
        env = self.git_env.copy()
        env.update({
            "AGENT_BIN": str(self.fake),
            "FAKE_ROOT": str(self.root),
            "INDEX_ROOT": str(self.index),
            "MARKETPLACE_INDEX_ROOT": str(self.index),
            "MARKETPLACE_GIT_DIR": str(self.repo / ".git"),
            "CURSOR_API_KEY": "test-not-a-real-key",
            "HOME": str(self.root),
        })
        return subprocess.run(
            [str(SCRIPT), self.sha],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def argv_log(self) -> list[list[str]]:
        path = self.root / "argv.log"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assert_no_update_or_ref(self, proc: subprocess.CompletedProcess[str]) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("plugin marketplace update", text)
        self.assertIn("set -euo pipefail", text)
        for args in self.argv_log():
            self.assertNotIn("update", args)
            self.assertNotIn("--git-ref", args)
        self.assertNotIn("test-not-a-real-key", proc.stdout + proc.stderr)

    def test_already_indexed_commit_does_not_remove(self) -> None:
        self.write_state([self.registration(self.sha)], "stale")
        self.seed_index(self.sha)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"OK index {self.sha} context-ledger 0.1.19", proc.stdout)
        commands = [args[2] for args in self.argv_log() if len(args) > 2]
        self.assertEqual(commands, ["list"])
        self.assert_no_update_or_ref(proc)

    def test_stale_index_removes_then_adds_without_git_ref(self) -> None:
        self.write_state([self.registration(self.old)], "stale")
        self.seed_index(self.old)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REINDEX", proc.stderr)
        self.assertIn("context-ledger 0.1.19", proc.stdout)
        commands = [args[2] for args in self.argv_log() if len(args) > 2]
        self.assertEqual(commands, ["list", "remove", "add", "list"])
        add = [args for args in self.argv_log() if len(args) > 2 and args[2] == "add"][0]
        self.assertNotIn("--git-ref", add)
        self.assertIn("https://github.com/ingpoc/agent-plugins", add)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["marketplaces"]), 1)
        self.assertEqual(state["marketplaces"][0]["gitRef"], self.sha)
        self.assert_no_update_or_ref(proc)

    def test_second_add_can_recover_one_failed_add(self) -> None:
        self.write_state([self.registration(self.old)], "fail_once")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        commands = [args[2] for args in self.argv_log() if len(args) > 2]
        self.assertEqual(commands, ["list", "remove", "add", "add", "list"])
        self.assert_no_update_or_ref(proc)

    def test_version_mismatch_fails_and_keeps_one_registration(self) -> None:
        self.write_state(
            [self.registration(self.old)],
            "stale",
            indexed_version="0.0.1",
        )
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL:", proc.stderr)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["marketplaces"]), 1)
        self.assert_no_update_or_ref(proc)

    def test_zero_plugins_fails(self) -> None:
        self.write_state([self.registration(self.old)], "zero_plugins")
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("plugins=0", proc.stderr)
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["marketplaces"]), 1)
        self.assert_no_update_or_ref(proc)

    def test_two_registrations_are_not_removed(self) -> None:
        self.write_state(
            [self.registration(self.old), self.registration(self.sha)],
            "stale",
        )
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        commands = [args[2] for args in self.argv_log() if len(args) > 2]
        self.assertEqual(commands, ["list"])
        self.assert_no_update_or_ref(proc)

    def test_zero_registrations_are_not_replaced_silently(self) -> None:
        self.write_state([], "stale")
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        commands = [args[2] for args in self.argv_log() if len(args) > 2]
        self.assertEqual(commands, ["list"])
        self.assert_no_update_or_ref(proc)

    def test_failed_add_restores_instead_of_leaving_zero(self) -> None:
        self.write_state([self.registration(self.old)], "fail_add")
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("restore add failed", proc.stderr)
        commands = [args[2] for args in self.argv_log() if len(args) > 2]
        self.assertGreaterEqual(commands.count("add"), 2)
        self.assertNotIn("update", commands)
        self.assert_no_update_or_ref(proc)


if __name__ == "__main__":
    unittest.main()
