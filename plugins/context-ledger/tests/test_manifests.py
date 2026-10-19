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
PORTABLE_TAIL = [
    "context-ledger-mcp",
    "${PLUGIN_ROOT}",
    "${CURSOR_PLUGIN_ROOT}",
    "${CLAUDE_PLUGIN_ROOT}",
    "serve",
]
CURSOR_TAIL = [
    "context-ledger-mcp",
    "${CURSOR_PLUGIN_ROOT}",
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
        version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
        for rel in ("mcp.json", ".cursor-plugin/mcp.json"):
            server = _servers(ROOT / rel)["context-ledger"]
            script = server["args"][1]
            self.assertNotIn("${", script, rel)
            self.assertNotIn("|| true", script, rel)
            self.assertEqual(script.count(version), 1, rel)
            self.assertIn(f"ver={version}", script, rel)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "resolver.sh"
                path.write_text(script, encoding="utf-8")
                proc = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

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
                if kind == "claude":
                    self.assertTrue(command.startswith("${CLAUDE_PLUGIN_ROOT}/"), (path, command))
                else:
                    self.assertEqual(command, "sh", path)
                    self.assertNotIn("/", command, path)
                    self.assertFalse(command.startswith("."), path)
                    self.assertIsNone(server.get("cwd"), path)
                cwd = server.get("cwd", None)
                self.assertNotIn(cwd, ("./", ".", "bin"), path)
                if cwd is None:
                    continue
                if kind == "claude":
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
        self.assertEqual(context_ledger.__version__, "0.1.34")
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
            kind = _classify(path)
            if kind == "portable":
                self.assertEqual(command, "sh")
                self.assertEqual(args[0], "-c")
                self.assertEqual(args[2:], PORTABLE_TAIL)
                self.assertNotIn("${", args[1])
                self.assertEqual(server["env"], CLIENT_TOKEN_ENV)
            elif kind == "cursor":
                self.assertEqual(command, "sh")
                self.assertEqual(args[0], "-c")
                self.assertEqual(args[2:], CURSOR_TAIL)
                self.assertNotIn("${", args[1])
                self.assertIsNone(server.get("cwd"))
                self.assertEqual(server["env"], CLIENT_TOKEN_ENV)
            else:
                self.assertTrue(str(command).endswith("/bin/context-ledger-mcp"), command)
                self.assertEqual(args, ["serve"])

    def test_cursor_and_codex_interpolation(self) -> None:
        server = _servers(ROOT / "mcp.json")["context-ledger"]
        args = list(server["args"])
        self.assertEqual(args[0], "-c")
        self.assertEqual(args[2:], PORTABLE_TAIL)
        self.assertNotIn("${", args[1])
        self.assertEqual(server["env"], CLIENT_TOKEN_ENV)
        root = "/plugin/context-ledger"
        codex_args = [codex_expand(arg, root) for arg in args]
        self.assertEqual(codex_args[1], args[1])
        self.assertEqual(codex_args[3], root)
        self.assertEqual(codex_args[4], "${CURSOR_PLUGIN_ROOT}")
        self.assertEqual(codex_args[5], "${CLAUDE_PLUGIN_ROOT}")
        self.assertEqual(codex_expand(server["env"]["CONTEXT_LEDGER_MCP_CLIENT_TOKEN"], root), CLIENT_TOKEN_PLACEHOLDER)
        cursor_args = [cursor_expand(arg, root, {}) for arg in args]
        self.assertEqual(cursor_args[1], args[1])
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
        self.assertEqual(template[0], "-c")
        self.assertEqual(template[2:], PORTABLE_TAIL)

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
            version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
            for directory in (fake, other):
                (directory / "plugin.json").write_text(
                    json.dumps({"name": "context-ledger", "version": version}) + "\n",
                    encoding="utf-8",
                )
            empty = base / "empty"
            empty.mkdir()
            home = base / "home"
            home.mkdir()
            stub = base / "stubbin"
            stub.mkdir()
            curl = stub / "curl"
            curl.write_text("#!/bin/sh\nprintf '%s\\n' 'curl failed' >&2\nexit 1\n", encoding="utf-8")
            curl.chmod(0o755)
            root = str(fake)

            def run(args: list[str], extra: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
                env = {
                    "PATH": str(stub) + os.pathsep + "/usr/bin:/bin",
                    "HOME": str(home),
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
            self.assertEqual(codex_args[1], template[1])
            self.assertEqual(codex_args[3], root)
            self.assertEqual(cursor_args[1], template[1])
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
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing.stdout, "")
        payload = json.loads(missing.stderr)
        self.assertEqual(payload["error"]["code"], "PLUGIN_BOOTSTRAP_FAILED")
        self.assertIn("download", payload["error"]["message"])
        self.assertIn(f"context-ledger-v{version}", payload["error"]["message"])

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


class ResolverFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        if _collection_root() is None:
            self.skipTest("client-transformed copy is not a portable package")

    def _servers(self) -> dict[str, dict]:
        return {
            "portable": _servers(ROOT / "mcp.json")["context-ledger"],
            "cursor": _servers(ROOT / ".cursor-plugin/mcp.json")["context-ledger"],
        }

    def _invoke(
        self,
        server: dict,
        args: list[str],
        home: Path,
        cwd: Path,
        path: str,
        extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {"PATH": path, "HOME": str(home), "PWD": str(cwd)}
        if extra:
            env.update(extra)
        return subprocess.run(
            [server["command"], *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_cache_highest_version_beats_newer_mtime_and_root_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            cwd = base / "cwd"
            home.mkdir()
            cwd.mkdir()

            def plant(directory: Path, version: str, name: str = "context-ledger") -> Path:
                launcher = directory / "bin" / "context-ledger-mcp"
                launcher.parent.mkdir(parents=True)
                launcher.write_text(
                    "#!/bin/sh\nprintf '%s\\n' \"$0\" >&2\nprintf '%s\\n' \"$@\" >&2\nexit 0\n",
                    encoding="utf-8",
                )
                launcher.chmod(0o755)
                (directory / "plugin.json").write_text(
                    json.dumps({"name": name, "version": version}) + "\n",
                    encoding="utf-8",
                )
                return launcher

            version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
            stale = plant(
                home / ".cursor/plugins/cache/ingpoc-agent-plugins/context-ledger/oldsha",
                "0.1.0",
            )
            current = plant(
                home / ".cursor/plugins/cache/ingpoc-agent-plugins/context-ledger/newsha",
                version,
            )
            decoy = plant(
                home / ".cursor/plugins/cache/ingpoc-agent-plugins/7/other",
                "9.9.9",
                name="other-plugin",
            )
            os.utime(stale, (2_200_000_000, 2_200_000_000))
            os.utime(current, (1_000_000_000, 1_000_000_000))
            os.utime(decoy, (2_100_000_000, 2_100_000_000))
            real = plant(base / "real-root", version)
            path = "/usr/bin:/bin"
            expected = f"{current}\nserve\n"
            root_expected = f"{real}\nserve\n"
            for server in self._servers().values():
                literal = self._invoke(server, list(server["args"]), home, cwd, path)
                self.assertEqual(literal.returncode, 0, literal.stderr)
                self.assertEqual(literal.stdout, "")
                self.assertEqual(literal.stderr, expected)
                expanded = list(server["args"])
                expanded[3] = str(real.parent.parent)
                chosen = self._invoke(server, expanded, home, cwd, path)
                self.assertEqual(chosen.returncode, 0, chosen.stderr)
                self.assertEqual(chosen.stdout, "")
                self.assertEqual(chosen.stderr, root_expected)

    def test_stale_materialized_copy_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            cwd = base / "cwd"
            home.mkdir()
            cwd.mkdir()
            stub = base / "stub"
            stub.mkdir()
            curl = stub / "curl"
            curl.write_text("#!/bin/sh\nprintf '%s\\n' 'curl failed' >&2\nexit 1\n", encoding="utf-8")
            curl.chmod(0o755)
            path = str(stub) + os.pathsep + "/usr/bin:/bin"

            def plant(directory: Path, version: str) -> Path:
                launcher = directory / "bin" / "context-ledger-mcp"
                launcher.parent.mkdir(parents=True)
                launcher.write_text(
                    "#!/bin/sh\nprintf '%s\\n' \"$0\" >&2\nprintf '%s\\n' \"$@\" >&2\nexit 0\n",
                    encoding="utf-8",
                )
                launcher.chmod(0o755)
                (directory / "plugin.json").write_text(
                    json.dumps({"name": "context-ledger", "version": version}) + "\n",
                    encoding="utf-8",
                )
                return launcher

            stale = plant(
                home / ".cursor/plugins/cache/ingpoc-agent-plugins/context-ledger/oldsha",
                "0.1.17",
            )
            stale_root = plant(base / "stale-root", "0.1.17")
            for server in self._servers().values():
                cached = self._invoke(server, list(server["args"]), home, cwd, path)
                self.assertNotEqual(cached.returncode, 0)
                self.assertEqual(cached.stdout, "")
                self.assertNotIn(str(stale), cached.stderr)
                payload = json.loads(cached.stderr)
                self.assertEqual(payload["error"]["code"], "PLUGIN_BOOTSTRAP_FAILED")
                args = list(server["args"])
                args[3] = str(stale_root.parent.parent)
                rooted = self._invoke(server, args, home, cwd, path)
                self.assertNotEqual(rooted.returncode, 0, rooted.stderr)
                self.assertEqual(rooted.stdout, "")
                self.assertNotIn(str(stale_root), rooted.stderr)
                rooted_payload = json.loads(rooted.stderr)
                self.assertEqual(rooted_payload["error"]["code"], "PLUGIN_BOOTSTRAP_FAILED")

    def test_bootstrap_failure_and_tarball(self) -> None:
        version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
        url = (
            "https://codeload.github.com/ingpoc/agent-plugins/tar.gz/refs/tags/"
            f"context-ledger-v{version}"
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cwd = base / "cwd"
            cwd.mkdir()
            stub = base / "stub"
            stub.mkdir()
            curl = stub / "curl"
            path = str(stub) + os.pathsep + "/usr/bin:/bin"

            def write_curl(body: str) -> None:
                curl.write_text(body, encoding="utf-8")
                curl.chmod(0o755)

            write_curl("#!/bin/sh\nprintf '%s\\n' 'curl failed' >&2\nexit 1\n")
            for rel in ("mcp.json", ".cursor-plugin/mcp.json"):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertEqual(text.count(version), 1, rel)
            for server in self._servers().values():
                home = base / f"fail-{id(server)}"
                home.mkdir()
                proc = self._invoke(server, list(server["args"]), home, cwd, path)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(proc.stderr.count("\n"), 1)
                payload = json.loads(proc.stderr)
                self.assertEqual(payload["error"]["code"], "PLUGIN_BOOTSTRAP_FAILED")
                self.assertIn("download", payload["error"]["message"])
                self.assertIn(url, payload["error"]["message"])

            def archive(version_in_tarball: str) -> Path:
                import io
                import tarfile

                dest = base / f"src-{version_in_tarball}.tar.gz"
                prefix = f"agent-plugins-context-ledger-v{version}/plugins/context-ledger"
                body = b"#!/bin/sh\nprintf '%s\\n' \"$0\" >&2\nprintf '%s\\n' \"$@\" >&2\nexit 0\n"
                plugin = (json.dumps({"name": "context-ledger", "version": version_in_tarball}) + "\n").encode()
                with tarfile.open(dest, "w:gz") as tar:
                    for name in (
                        f"agent-plugins-context-ledger-v{version}/",
                        f"agent-plugins-context-ledger-v{version}/plugins/",
                        prefix + "/",
                        prefix + "/bin/",
                    ):
                        info = tarfile.TarInfo(name)
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        tar.addfile(info)
                    info = tarfile.TarInfo(prefix + "/plugin.json")
                    info.size = len(plugin)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(plugin))
                    info = tarfile.TarInfo(prefix + "/bin/context-ledger-mcp")
                    info.size = len(body)
                    info.mode = 0o755
                    tar.addfile(info, io.BytesIO(body))
                    extra = b'{"name":"other"}\n'
                    info = tarfile.TarInfo(
                        f"agent-plugins-context-ledger-v{version}/plugins/other/plugin.json"
                    )
                    info.size = len(extra)
                    tar.addfile(info, io.BytesIO(extra))
                return dest

            good = archive(version)
            bad = archive("0.0.1")
            write_curl(
                "#!/bin/sh\n"
                "out=\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"-o\" ]; then shift; out=$1; fi\n"
                "  if [ $# -gt 0 ]; then shift; fi\n"
                "done\n"
                "if [ -z \"$out\" ]; then printf '%s\\n' 'missing -o' >&2; exit 2; fi\n"
                "cat \"$CL_TARBALL\" > \"$out\"\n"
                "exit 0\n"
            )
            for server in self._servers().values():
                home = base / f"boot-{id(server)}"
                home.mkdir()
                proc = self._invoke(
                    server,
                    list(server["args"]),
                    home,
                    cwd,
                    path,
                    {"CL_TARBALL": str(good)},
                )
                dest = home / ".cache/context-ledger" / f"plugin-{version}" / "bin/context-ledger-mcp"
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(proc.stderr, f"{dest}\nserve\n")
                self.assertTrue(dest.is_file())
                self.assertFalse((home / ".cache/context-ledger" / f"plugin-{version}" / "other").exists())
                wrong_home = base / f"bad-{id(server)}"
                wrong_home.mkdir()
                wrong = self._invoke(
                    server,
                    list(server["args"]),
                    wrong_home,
                    cwd,
                    path,
                    {"CL_TARBALL": str(bad)},
                )
                self.assertNotEqual(wrong.returncode, 0)
                self.assertEqual(wrong.stdout, "")
                wrong_payload = json.loads(wrong.stderr)
                self.assertEqual(wrong_payload["error"]["code"], "PLUGIN_BOOTSTRAP_FAILED")
                self.assertIn("verify", wrong_payload["error"]["message"])
                self.assertIn(url, wrong_payload["error"]["message"])
                self.assertFalse((wrong_home / ".cache/context-ledger" / f"plugin-{version}").exists())


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


class ClientScriptTests(unittest.TestCase):
    def _plant(self, directory: Path, version: str) -> None:
        launcher = directory / "bin" / "context-ledger-mcp"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        (directory / "plugin.json").write_text(
            json.dumps({"name": "context-ledger", "version": version}) + "\n",
            encoding="utf-8",
        )

    def _run(self, home: Path, script: str, *args: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        if extra:
            env.update(extra)
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / script), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_install_refresh_and_access_do_not_create_a_ledger(self) -> None:
        version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for client, dest in (
                ("cursor", home / ".cursor/plugins/local/context-ledger"),
                ("codex", home / ".agents/plugins/context-ledger"),
                ("grok", home / ".grok/plugins/context-ledger"),
            ):
                missing = self._run(home, "refresh-client.sh", client)
                self.assertNotEqual(missing.returncode, 0, missing.stderr)
                self.assertIn(f"no context-ledger install for {client}", missing.stderr)
                installed = self._run(home, "install-client.sh", client)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                self.assertIn(f"version={version}", installed.stdout)
                self.assertEqual(
                    json.loads((dest / "plugin.json").read_text(encoding="utf-8"))["version"],
                    version,
                )
                refreshed = self._run(home, "refresh-client.sh", client)
                self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
                self.assertFalse((home / ".context-ledger").exists())

    def test_refresh_replaces_stale_cache_in_place(self) -> None:
        version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stale = home / ".cursor/plugins/cache/ingpoc-agent-plugins/context-ledger/oldsha"
            self._plant(stale, "0.1.17")
            proc = self._run(home, "refresh-client.sh", "cursor")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                json.loads((stale / "plugin.json").read_text(encoding="utf-8"))["version"],
                version,
            )
            self.assertFalse((home / ".cursor/plugins/local/context-ledger").exists())
            self.assertFalse((home / ".context-ledger").exists())
            siblings = list((home / ".cursor/plugins/cache/ingpoc-agent-plugins/context-ledger").iterdir())
            self.assertEqual([path.name for path in siblings], ["oldsha"])

    def test_access_check_fails_loud_without_a_second_root(self) -> None:
        secret = "token-must-not-appear"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            unbound = self._run(home, "check-ledger-access.sh")
            self.assertNotEqual(unbound.returncode, 0)
            self.assertIn("LEDGER_UNBOUND", unbound.stderr)
            self.assertFalse((home / ".context-ledger").exists())
            ledger = home / "ledger"
            ledger.mkdir()
            data = home / "data"
            data.mkdir()
            (data / "binding.json").write_text(
                json.dumps({"ledger_dir": str(ledger), "ledger_id": "x"}) + "\n",
                encoding="utf-8",
            )
            missing = self._run(home, "check-ledger-access.sh", "--data", str(data))
            self.assertNotEqual(missing.returncode, 0, missing.stderr)
            self.assertIn(str(ledger / "ledger.sqlite3"), missing.stderr)
            self.assertFalse((ledger / "ledger.sqlite3").exists())
            (ledger / "ledger.sqlite3").write_bytes(b"ok")
            ready = self._run(home, "check-ledger-access.sh", "--data", str(data))
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertEqual(ready.stdout, "ok local\n")
            empty = home / "empty-home"
            empty.mkdir()
            remote = self._run(
                home,
                "check-ledger-access.sh",
                extra={
                    "CONTEXT_LEDGER_MCP_TOKEN": secret,
                    "CONTEXT_LEDGER_MCP_URL": "http://127.0.0.1:9/mcp",
                    "CONTEXT_LEDGER_MCP_TIMEOUT": "0.2",
                    "HOME": str(empty),
                },
            )
            self.assertNotEqual(remote.returncode, 0, remote.stderr)
            self.assertNotIn(secret, remote.stdout)
            self.assertNotIn(secret, remote.stderr)
            self.assertFalse((home / "empty-home" / ".context-ledger").exists())


if __name__ == "__main__":
    unittest.main()
