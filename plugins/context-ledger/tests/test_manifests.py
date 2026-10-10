"""Shipped MCP manifests, root resolver, and serve-mode keychain contract."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "context-ledger-mcp"
DESCRIPTION = "Scoped, local decision traces with sparse capture and progressive retrieval."

# Same string as plugins/context-ledger/mcp.json args[1]. Drift fails this file.
RESOLVER = (
    'root=; c=$1; case "$c" in ""|\\$*) ;; *) '
    'if [ -x "$c/bin/context-ledger-mcp" ]; then root=$c; fi ;; esac; '
    'if [ -z "$root" ] && [ -n "$PLUGIN_ROOT" ] && [ -x "$PLUGIN_ROOT/bin/context-ledger-mcp" ]; then root=$PLUGIN_ROOT; fi; '
    'if [ -z "$root" ] && [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -x "$CLAUDE_PLUGIN_ROOT/bin/context-ledger-mcp" ]; then root=$CLAUDE_PLUGIN_ROOT; fi; '
    'if [ -z "$root" ] && [ -n "$CURSOR_PLUGIN_ROOT" ] && [ -x "$CURSOR_PLUGIN_ROOT/bin/context-ledger-mcp" ]; then root=$CURSOR_PLUGIN_ROOT; fi; '
    'if [ -z "$root" ] && [ -n "$PWD" ] && [ -x "$PWD/bin/context-ledger-mcp" ]; then root=$PWD; fi; '
    'if [ -n "$root" ]; then shift; exec "$root/bin/context-ledger-mcp" "$@"; fi; '
    "echo '{\"ok\":false,\"error\":{\"code\":\"PLUGIN_ROOT_UNRESOLVED\",\"retryable\":false,\"message\":\"client must expand PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT/CURSOR_PLUGIN_ROOT, or load the package via .cursor-plugin/ or .claude-plugin/\"}}' >&2; "
    "exit 127"
)

CURSOR_INTERP = re.compile(
    r"\$\{(?:env:([A-Za-z_][A-Za-z0-9_]*)|([^:}]+)(?::-([^}]*))?)\}"
)
KEYCHAIN_TOKEN = "kc-token-7f3e9a"


def _collection_root() -> Path | None:
    env = os.environ.get("AGENT_PLUGINS_COLLECTION")
    candidates = [Path(env)] if env else []
    candidates.append(ROOT.parent.parent)
    for candidate in candidates:
        if not (candidate / "AGENTS.md").is_file():
            continue
        if not (candidate / "plugins/context-ledger/plugin.json").is_file():
            continue
        if not (candidate / ".cursor-plugin/marketplace.json").is_file():
            continue
        return candidate
    return None


def cursor_interpolate(value: str, lookup: dict[str, str] | None = None) -> str:
    """Cursor CLI string interpolation with an explicit lookup map."""
    env = lookup or {}

    def repl(match: re.Match[str]) -> str:
        env_name, name, default = match.group(1), match.group(2), match.group(3)
        if env_name is not None:
            if env_name in env:
                return env[env_name]
            return match.group(0)
        if name in env:
            return env[name]
        if default is not None:
            return default
        return match.group(0)

    return CURSOR_INTERP.sub(repl, value)


def _classify(path: Path) -> str:
    parts = path.relative_to(ROOT).parts
    if parts and parts[0] == ".cursor-plugin":
        return "cursor"
    if parts and parts[0] == ".claude-plugin":
        return "claude"
    return "portable"


def _shipped_mcp_manifests() -> list[Path]:
    found: list[Path] = []
    for rel in ("mcp.json", ".mcp.json"):
        path = ROOT / rel
        if path.is_file():
            found.append(path)
    for manifest_path in (
        ROOT / "plugin.json",
        ROOT / ".cursor-plugin" / "plugin.json",
        ROOT / ".claude-plugin" / "plugin.json",
    ):
        if not manifest_path.is_file():
            continue
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        ref = data.get("mcpServers")
        if isinstance(ref, str) and not ref.startswith("/"):
            found.append((ROOT / ref).resolve())
    for base in (ROOT / ".cursor-plugin", ROOT / ".claude-plugin"):
        if not base.is_dir():
            continue
        found.extend(path.resolve() for path in base.glob("*mcp*.json") if path.is_file())
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _servers(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise AssertionError(f"{path} has no mcpServers object")
    return servers


class ManifestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        if _collection_root() is None:
            self.skipTest("client-transformed copy is not a portable package")

    def test_resolver_constant_matches_manifest(self) -> None:
        server = _servers(ROOT / "mcp.json")["context-ledger"]
        self.assertEqual(server["args"][1], RESOLVER)
        self.assertNotIn("${", RESOLVER)

    def test_every_stdio_manifest_is_absolute_to_its_client_root(self) -> None:
        manifests = _shipped_mcp_manifests()
        names = {path.relative_to(ROOT).as_posix() for path in manifests}
        self.assertIn("mcp.json", names)
        self.assertIn(".cursor-plugin/mcp.json", names)
        self.assertIn(".claude-plugin/mcp.json", names)
        for path in manifests:
            kind = _classify(path)
            for name, server in _servers(path).items():
                self.assertNotIn("headers", server, path)
                transport = server.get("type")
                self.assertEqual(transport, "stdio", (path, name))
                self.assertNotIn(transport, ("http", "sse", "streamable-http"))
                command = server.get("command")
                self.assertIsInstance(command, str)
                assert isinstance(command, str)
                if kind == "portable":
                    self.assertNotIn("/", command, path)
                    self.assertFalse(command.startswith("."), path)
                else:
                    prefix = (
                        "${CURSOR_PLUGIN_ROOT}/"
                        if kind == "cursor"
                        else "${CLAUDE_PLUGIN_ROOT}/"
                    )
                    self.assertTrue(command.startswith(prefix), (path, command))
                cwd = server.get("cwd", None)
                self.assertNotIn(cwd, ("./", ".", "bin"), path)
                if cwd is None:
                    continue
                if kind == "cursor":
                    self.assertEqual(cwd, "${CURSOR_PLUGIN_ROOT}")
                elif kind == "claude":
                    self.assertEqual(cwd, "${CLAUDE_PLUGIN_ROOT}")
                else:
                    self.assertIn(cwd, ("${PLUGIN_ROOT}", "${PLUGIN_DATA}"))

    def test_manifests_agree_on_server_version_and_description(self) -> None:
        sys.path.insert(0, str(ROOT))
        import context_ledger

        versions = [
            json.loads((ROOT / rel).read_text(encoding="utf-8"))["version"]
            for rel in (
                "plugin.json",
                ".cursor-plugin/plugin.json",
                ".claude-plugin/plugin.json",
            )
        ]
        self.assertEqual(versions, [context_ledger.__version__] * 3)
        self.assertEqual(context_ledger.__version__, "0.1.15")
        descriptions = [
            json.loads((ROOT / rel).read_text(encoding="utf-8"))["description"]
            for rel in (
                "plugin.json",
                ".cursor-plugin/plugin.json",
                ".claude-plugin/plugin.json",
            )
        ]
        self.assertEqual(descriptions, [DESCRIPTION] * 3)
        for path in _shipped_mcp_manifests():
            servers = _servers(path)
            self.assertEqual(set(servers), {"context-ledger"}, path)
            server = servers["context-ledger"]
            self.assertEqual(server["type"], "stdio")
            command = server["command"]
            args = server["args"]
            if command == "sh":
                self.assertEqual(args[0], "-c")
                self.assertIn('exec "$root/bin/context-ledger-mcp"', args[1])
                self.assertEqual(args[args.index("${PLUGIN_ROOT}") + 1 :], ["serve"])
            else:
                self.assertTrue(str(command).endswith("/bin/context-ledger-mcp"), command)
                self.assertEqual(args, ["serve"])

    def test_cursor_and_codex_interpolation_touch_only_the_root_arg(self) -> None:
        args = list(_servers(ROOT / "mcp.json")["context-ledger"]["args"])
        self.assertEqual(args[1], RESOLVER)
        cursor_args = [cursor_interpolate(arg, {}) for arg in args]
        self.assertEqual(cursor_args[1], RESOLVER)
        self.assertEqual(cursor_args, args)
        self.assertEqual(cursor_interpolate("${MISSING:-fallback}", {}), "fallback")
        self.assertEqual(cursor_interpolate("${MISSING}", {}), "${MISSING}")
        root = "/plugin/context-ledger"
        codex_args = [arg.replace("${PLUGIN_ROOT}", root) for arg in args]
        changed = [index for index, (before, after) in enumerate(zip(args, codex_args)) if before != after]
        self.assertEqual(changed, [args.index("${PLUGIN_ROOT}")])
        self.assertEqual(codex_args[1], RESOLVER)
        self.assertEqual(codex_args[args.index("${PLUGIN_ROOT}")], root)

    def test_resolver_picks_the_first_real_root(self) -> None:
        server = _servers(ROOT / "mcp.json")["context-ledger"]
        template = list(server["args"])
        self.assertEqual(template[1], RESOLVER)
        root_at = template.index("${PLUGIN_ROOT}")

        def argv(root_arg: str | None) -> list[str]:
            args = list(template)
            if root_arg is not None:
                args[root_at] = root_arg
            return [server["command"], *args]

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake = base / "plugin"
            launcher = fake / "bin" / "context-ledger-mcp"
            launcher.parent.mkdir(parents=True)
            launcher.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$@"\n'
                'printf "MARKER=%s\\n" "$CL_MARKER"\n',
                encoding="utf-8",
            )
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            empty = base / "empty"
            empty.mkdir()

            def run(root_arg: str | None, extra: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "CL_MARKER": "resolved",
                    "PWD": str(cwd),
                }
                env.update(extra)
                return subprocess.run(
                    argv(root_arg),
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                )

            via_arg = run(str(fake), {}, empty)
            via_plugin = run(None, {"PLUGIN_ROOT": str(fake)}, empty)
            via_claude = run(None, {"CLAUDE_PLUGIN_ROOT": str(fake)}, empty)
            via_cursor = run(None, {"CURSOR_PLUGIN_ROOT": str(fake)}, empty)
            via_pwd = run(None, {}, fake)
            missing = run(None, {}, empty)

        expected = "serve\nMARKER=resolved\n"
        for proc in (via_arg, via_plugin, via_claude, via_cursor, via_pwd):
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, expected)
            self.assertEqual(proc.stderr, "")
        self.assertEqual(missing.returncode, 127)
        self.assertEqual(missing.stdout, "")
        self.assertIn("PLUGIN_ROOT_UNRESOLVED", missing.stderr)
        payload = json.loads(missing.stderr)
        self.assertEqual(payload["error"]["code"], "PLUGIN_ROOT_UNRESOLVED")
        self.assertIn(".cursor-plugin/", payload["error"]["message"])
        self.assertIn(".claude-plugin/", payload["error"]["message"])

    def test_shell_files_have_no_or_true(self) -> None:
        files: list[str] = []
        for base in (ROOT / "bin", ROOT / "scripts"):
            for path in sorted(base.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                if path.suffix not in {".sh", ""}:
                    continue
                raw = path.read_bytes()
                first = raw.splitlines()[0] if raw else b""
                if path.suffix != ".sh" and not (first.startswith(b"#!") and b"sh" in first):
                    continue
                files.append(str(path.relative_to(ROOT)))
        self.assertTrue(files)
        proc = subprocess.run(
            ["grep", "-n", "|| true", *files],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.returncode, 1)


class LauncherModeTests(unittest.TestCase):
    def test_keychain_token_selects_the_bridge_without_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            data = base / "data"
            home.mkdir()
            stub = base / "stubbin"
            stub.mkdir()
            security = stub / "security"
            security.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' '{KEYCHAIN_TOKEN}'\n",
                encoding="utf-8",
            )
            security.chmod(0o755)
            fake_py = base / "fake-python"
            fake_py.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-u" ]; then\n'
                '  case "$2" in\n'
                "    */remote_bridge.py)\n"
                '      if [ -n "${CONTEXT_LEDGER_MCP_TOKEN:-}" ]; then\n'
                "        printf 'bridge\\n'\n"
                "        exit 0\n"
                "      fi\n"
                "      ;;\n"
                "  esac\n"
                "fi\n"
                "printf 'not-bridge\\n' >&2\n"
                "exit 4\n",
                encoding="utf-8",
            )
            fake_py.chmod(0o755)
            env = {
                "PATH": str(stub) + os.pathsep + "/usr/bin:/bin",
                "HOME": str(home),
                "CONTEXT_LEDGER_DATA": str(data),
                "CONTEXT_LEDGER_PYTHON": str(fake_py),
            }
            proc = subprocess.run(
                [str(LAUNCHER), "serve"],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertFalse(data.exists())
            self.assertFalse((home / ".context-ledger").exists())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "bridge\n")
        self.assertNotIn(KEYCHAIN_TOKEN, proc.stdout)
        self.assertNotIn(KEYCHAIN_TOKEN, proc.stderr)

    def test_keychain_miss_is_unbound_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            data = base / "data"
            home.mkdir()
            stub = base / "stubbin"
            stub.mkdir()
            security = stub / "security"
            security.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            security.chmod(0o755)
            env = {
                "PATH": str(stub) + os.pathsep + "/usr/bin:/bin",
                "HOME": str(home),
                "CONTEXT_LEDGER_DATA": str(data),
                "CONTEXT_LEDGER_PYTHON": sys.executable,
            }
            proc = subprocess.run(
                [str(LAUNCHER), "serve"],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertFalse(data.exists())
            self.assertFalse((home / ".context-ledger").exists())
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn(KEYCHAIN_TOKEN, proc.stdout)
        self.assertNotIn(KEYCHAIN_TOKEN, proc.stderr)
        payload = json.loads(proc.stderr)
        message = payload["error"]["message"]
        self.assertEqual(payload["error"]["code"], "LEDGER_UNBOUND")
        self.assertIn("bin/context_ledger.py init", message)
        self.assertIn("CONTEXT_LEDGER_MCP_TOKEN", message)
        self.assertIn("context-ledger.mcp", message)
        self.assertIn("bearer_token", message)


if __name__ == "__main__":
    unittest.main()
