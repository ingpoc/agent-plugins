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
    'root=; for c in "$1" "$2" "$3"; do case "$c" in ""|\\$*) ;; *) '
    'if [ -x "$c/bin/context-ledger-mcp" ]; then root=$c; break; fi ;; esac; done; '
    'if [ -z "$root" ] && [ -n "$PLUGIN_ROOT" ] && [ -x "$PLUGIN_ROOT/bin/context-ledger-mcp" ]; then root=$PLUGIN_ROOT; fi; '
    'if [ -z "$root" ] && [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -x "$CLAUDE_PLUGIN_ROOT/bin/context-ledger-mcp" ]; then root=$CLAUDE_PLUGIN_ROOT; fi; '
    'if [ -z "$root" ] && [ -n "$CURSOR_PLUGIN_ROOT" ] && [ -x "$CURSOR_PLUGIN_ROOT/bin/context-ledger-mcp" ]; then root=$CURSOR_PLUGIN_ROOT; fi; '
    'if [ -z "$root" ] && [ -n "$PWD" ] && [ -x "$PWD/bin/context-ledger-mcp" ]; then root=$PWD; fi; '
    'if [ -n "$root" ]; then shift 3; exec "$root/bin/context-ledger-mcp" "$@"; fi; '
    "echo '{\"ok\":false,\"error\":{\"code\":\"PLUGIN_ROOT_UNRESOLVED\",\"retryable\":false,\"message\":\"client must expand PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT/CURSOR_PLUGIN_ROOT, or load the package via .cursor-plugin/ or .claude-plugin/\"}}' >&2; "
    "exit 127"
)
PORTABLE_ARGS = [
    "-c",
    RESOLVER,
    "context-ledger-mcp",
    "${PLUGIN_ROOT}",
    "${CURSOR_PLUGIN_ROOT}",
    "${CLAUDE_PLUGIN_ROOT}",
    "serve",
]
CLIENT_TOKEN_ENV = {
    "CONTEXT_LEDGER_MCP_CLIENT_TOKEN": "${env:CONTEXT_LEDGER_MCP_TOKEN}",
}
CLIENT_TOKEN_PLACEHOLDER = "${env:CONTEXT_LEDGER_MCP_TOKEN}"

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


def cursor_expand(value: str, plugin_root: str, lookup: dict[str, str] | None = None) -> str:
    """Cursor: replace root tokens, then ${env:NAME} / ${NAME} / ${NAME:-default}."""
    expanded = value.replace("${CURSOR_PLUGIN_ROOT}", plugin_root).replace(
        "${CLAUDE_PLUGIN_ROOT}", plugin_root
    )
    return cursor_interpolate(expanded, lookup)


def codex_expand(value: str, plugin_root: str, plugin_data: str | None = None) -> str:
    """Codex expands only ${PLUGIN_ROOT} and ${PLUGIN_DATA}. Other ${...} stay literal."""
    expanded = value.replace("${PLUGIN_ROOT}", plugin_root)
    if plugin_data is not None:
        expanded = expanded.replace("${PLUGIN_DATA}", plugin_data)
    return expanded


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
                self.assertNotIn("url", server, path)
                env = server.get("env")
                if kind == "claude":
                    self.assertIsNone(env, path)
                    self.assertNotIn("${env:", path.read_text(encoding="utf-8"), path)
                else:
                    self.assertEqual(env, CLIENT_TOKEN_ENV, path)
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
        self.assertEqual(context_ledger.__version__, "0.1.16")
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
                self.assertEqual(args, PORTABLE_ARGS)
                self.assertEqual(server["env"], CLIENT_TOKEN_ENV)
            else:
                self.assertTrue(str(command).endswith("/bin/context-ledger-mcp"), command)
                self.assertEqual(args, ["serve"])

    def test_cursor_and_codex_interpolation(self) -> None:
        server = _servers(ROOT / "mcp.json")["context-ledger"]
        args = list(server["args"])
        self.assertEqual(args, PORTABLE_ARGS)
        self.assertEqual(server["env"], CLIENT_TOKEN_ENV)
        root = "/plugin/context-ledger"
        codex_args = [codex_expand(arg, root) for arg in args]
        self.assertEqual(codex_args[1], RESOLVER)
        self.assertEqual(codex_args[3], root)
        self.assertEqual(codex_args[4], "${CURSOR_PLUGIN_ROOT}")
        self.assertEqual(codex_args[5], "${CLAUDE_PLUGIN_ROOT}")
        self.assertEqual(codex_expand(server["env"]["CONTEXT_LEDGER_MCP_CLIENT_TOKEN"], root), CLIENT_TOKEN_PLACEHOLDER)
        cursor_args = [cursor_expand(arg, root, {}) for arg in args]
        self.assertEqual(cursor_args[1], RESOLVER)
        self.assertEqual(cursor_args[3], "${PLUGIN_ROOT}")
        self.assertEqual(cursor_args[4], root)
        self.assertEqual(cursor_args[5], root)
        self.assertEqual(cursor_expand(CLIENT_TOKEN_PLACEHOLDER, root, {}), CLIENT_TOKEN_PLACEHOLDER)
        self.assertEqual(
            cursor_expand(CLIENT_TOKEN_PLACEHOLDER, root, {"CONTEXT_LEDGER_MCP_TOKEN": "tok"}),
            "tok",
        )
        self.assertEqual(cursor_interpolate("${MISSING:-fallback}", {}), "fallback")
        self.assertEqual(cursor_interpolate("${MISSING}", {}), "${MISSING}")

    def test_resolver_picks_the_first_real_root(self) -> None:
        server = _servers(ROOT / "mcp.json")["context-ledger"]
        template = list(server["args"])
        self.assertEqual(template, PORTABLE_ARGS)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake = base / "plugin"
            other = base / "other"

            def plant(directory: Path, body: str) -> None:
                launcher = directory / "bin" / "context-ledger-mcp"
                launcher.parent.mkdir(parents=True)
                launcher.write_text("#!/bin/sh\n" + body, encoding="utf-8")
                launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            plant(fake, 'printf "%s\\n" "$@"\nprintf "MARKER=%s\\n" "$CL_MARKER"\n')
            plant(other, 'printf "second\\n"\n')
            empty = base / "empty"
            empty.mkdir()
            root = str(fake)

            def run(args: list[str], extra: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "CL_MARKER": "resolved",
                    "PWD": str(cwd),
                }
                env.update(extra)
                return subprocess.run(
                    [server["command"], *args],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                )

            codex_args = [codex_expand(arg, root) for arg in template]
            cursor_args = [cursor_expand(arg, root, {}) for arg in template]
            self.assertEqual(codex_args[1], RESOLVER)
            self.assertEqual(codex_args[3], root)
            self.assertEqual(cursor_args[1], RESOLVER)
            self.assertEqual(cursor_args[3], "${PLUGIN_ROOT}")
            self.assertEqual(cursor_args[4], root)
            self.assertEqual(cursor_args[5], root)
            first = list(template)
            first[3] = root
            first[4] = str(other)
            first[5] = str(other)
            via_codex = run(codex_args, {}, empty)
            via_cursor = run(cursor_args, {}, empty)
            via_plugin = run(template, {"PLUGIN_ROOT": root}, empty)
            via_claude = run(template, {"CLAUDE_PLUGIN_ROOT": root}, empty)
            via_cursor_env = run(template, {"CURSOR_PLUGIN_ROOT": root}, empty)
            via_pwd = run(template, {}, fake)
            via_first = run(first, {}, empty)
            missing = run(template, {}, empty)

        expected = "serve\nMARKER=resolved\n"
        for proc in (via_codex, via_cursor, via_plugin, via_claude, via_cursor_env, via_pwd, via_first):
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


def _write_probe(path: Path) -> None:
    """Fake python: bridge/local marker, no token text on stdout or stderr."""
    path.write_text(
        "#!/bin/sh\n"
        'if [ -n "${CONTEXT_LEDGER_MCP_CLIENT_TOKEN:-}" ]; then\n'
        "  printf 'client-lingers\\n' >&2\n"
        "  exit 5\n"
        "fi\n"
        'if [ "$1" = "-u" ]; then\n'
        '  case "$2" in\n'
        "    */remote_bridge.py)\n"
        '      case "${CONTEXT_LEDGER_MCP_TOKEN:-}" in\n'
        '        "")\n'
        "          ;;\n"
        "        '${env:CONTEXT_LEDGER_MCP_TOKEN}')\n"
        "          printf 'used-placeholder\\n' >&2\n"
        "          exit 6\n"
        "          ;;\n"
        "        *)\n"
        "          printf 'bridge\\n'\n"
        "          exit 0\n"
        "          ;;\n"
        "      esac\n"
        "      ;;\n"
        "    */context_ledger.py)\n"
        "      printf 'local\\n'\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        "printf 'not-bridge\\n' >&2\n"
        "exit 4\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


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
            _write_probe(fake_py)
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
        self.assertIn("CONTEXT_LEDGER_MCP_CLIENT_TOKEN", message)
        self.assertIn("context-ledger.mcp", message)
        self.assertIn("bearer_token", message)

    def test_literal_client_token_falls_through_to_keychain(self) -> None:
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
            _write_probe(fake_py)
            env = {
                "PATH": str(stub) + os.pathsep + "/usr/bin:/bin",
                "HOME": str(home),
                "CONTEXT_LEDGER_DATA": str(data),
                "CONTEXT_LEDGER_PYTHON": str(fake_py),
                "CONTEXT_LEDGER_MCP_CLIENT_TOKEN": CLIENT_TOKEN_PLACEHOLDER,
            }
            proc = subprocess.run(
                [str(LAUNCHER), "serve"],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "bridge\n")
        self.assertNotIn(KEYCHAIN_TOKEN, proc.stdout)
        self.assertNotIn(KEYCHAIN_TOKEN, proc.stderr)
        self.assertNotIn(CLIENT_TOKEN_PLACEHOLDER, proc.stdout)
        self.assertNotIn(CLIENT_TOKEN_PLACEHOLDER, proc.stderr)
        self.assertNotIn("client-lingers", proc.stderr)
        self.assertNotIn("used-placeholder", proc.stderr)

    def test_literal_client_token_without_keychain_is_unbound(self) -> None:
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
                "CONTEXT_LEDGER_MCP_CLIENT_TOKEN": CLIENT_TOKEN_PLACEHOLDER,
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
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        payload = json.loads(proc.stderr)
        self.assertEqual(payload["error"]["code"], "LEDGER_UNBOUND")
        self.assertNotIn(CLIENT_TOKEN_PLACEHOLDER, proc.stdout)
        self.assertNotIn(CLIENT_TOKEN_PLACEHOLDER, proc.stderr)

    def test_real_client_token_selects_the_bridge_without_leaking(self) -> None:
        client_token = "client-token-4b1c"
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
            fake_py = base / "fake-python"
            _write_probe(fake_py)
            env = {
                "PATH": str(stub) + os.pathsep + "/usr/bin:/bin",
                "HOME": str(home),
                "CONTEXT_LEDGER_DATA": str(data),
                "CONTEXT_LEDGER_PYTHON": str(fake_py),
                "CONTEXT_LEDGER_MCP_CLIENT_TOKEN": client_token,
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
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "bridge\n")
        self.assertNotIn(client_token, proc.stdout)
        self.assertNotIn(client_token, proc.stderr)
        self.assertNotIn("client-lingers", proc.stderr)

    def test_binding_unsets_client_token_before_local_stdio(self) -> None:
        client_token = "client-token-4b1c"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            data = base / "data"
            home.mkdir()
            data.mkdir()
            (data / "binding.json").write_text("{}\n", encoding="utf-8")
            fake_py = base / "fake-python"
            _write_probe(fake_py)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "CONTEXT_LEDGER_DATA": str(data),
                "CONTEXT_LEDGER_PYTHON": str(fake_py),
                "CONTEXT_LEDGER_MCP_CLIENT_TOKEN": client_token,
            }
            proc = subprocess.run(
                [str(LAUNCHER), "serve"],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "local\n")
        self.assertNotIn(client_token, proc.stdout)
        self.assertNotIn(client_token, proc.stderr)


if __name__ == "__main__":
    unittest.main()
