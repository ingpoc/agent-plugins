"""Small repo tools for Samantha. Shell checks are guardrails, not a sandbox."""

from __future__ import annotations

import hashlib
import fnmatch
import glob
import math
import ast
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from voice_cua.risk_gate import GATE

OUTPUT_CHARS = 4000
FILE_CHARS = 8000
DEFAULT_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 900.0
TURN_PORTS = ("8765", "8787", "8766")
_SECRET_ENV_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "COOKIE")
_STANDARD_BIN_DIRS = ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/opt/homebrew/bin")
_SHELL_CONTROL = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
                  "case", "esac", "function", "{", "}", "(", ")", "()"}
_SECRET_PATH = re.compile(
    r"(?i)(?:^|[/\s=])\.env[^/\s]*|(?:^|/)config/\.secret(?:/|$)|"
    r"(?:^|[/\s=])[^/\s]*\.pem(?:$|[/\s])|(?:^|[/\s=])id_[^/\s]+|"
    r"(?:^|/)Library/Keychains(?:/|$)|(?:^|[/\s])[^/\s]*\.keychain(?:-db)?(?:$|[/\s])"
)
_JOB_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}


def _shell_segments(command: str) -> list[str]:
    segments: list[str] = []
    start = 0
    quote = ""
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in {"'", '"'}:
            quote = ch
        elif ch in "\r\n;|" or (ch == "&" and i + 1 < len(command) and command[i + 1] == "&"):
            segments.append(command[start:i].strip())
            if ch in "|&" and i + 1 < len(command) and command[i + 1] == ch:
                i += 1
            start = i + 1
        i += 1
    segments.append(command[start:].strip())
    return [segment for segment in segments if segment]


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return []


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.normpath(path), os.path.normpath(root))) == os.path.normpath(root)
    except ValueError:
        return False


def _literal_path(value: str, *, cwd: str, home: str) -> str | None:
    if not value or any(ch in value for ch in "$`*?[]"):
        return None
    if value == "~":
        value = home
    elif value.startswith("~/"):
        value = os.path.join(home, value[2:])
    elif not os.path.isabs(value):
        value = os.path.join(cwd, value)
    return os.path.normpath(value)


def _path_pattern(value: str, *, cwd: str, home: str) -> str:
    home = os.path.realpath(home)
    cwd = os.path.realpath(cwd)
    value = value.replace("${HOME}", home).replace("$HOME", home)
    if value == "~":
        value = home
    elif value.startswith("~/"):
        value = os.path.join(home, value[2:])
    elif not os.path.isabs(value):
        value = os.path.join(cwd, value)
    return value


def _resolved_path(value: str, *, cwd: str, home: str) -> str | None:
    path = _literal_path(value, cwd=cwd, home=home)
    if path is None:
        return None
    try:
        return str(Path(path).resolve(strict=False))
    except (OSError, RuntimeError):
        return None


def _is_protected_path(path: str, *, home: str) -> bool:
    target = Path(path)
    typed_token = Path(home) / ".cache" / "macos-cua" / "typed-approve.token"
    activity_log = Path(home) / ".cache" / "macos-cua" / "samantha-activity.jsonl"
    return (target == typed_token or target == activity_log or target.name == "typed-approve.token"
            or bool(_SECRET_PATH.search(path)))


def _touches_protected_path(path: str, *, home: str) -> bool:
    target = Path(path)
    protected = (
        Path(home).resolve() / ".cache" / "macos-cua" / "typed-approve.token",
        Path(home).resolve() / ".cache" / "macos-cua" / "samantha-activity.jsonl",
    )
    return (_is_protected_path(path, home=home)
            or any(target == item or target in item.parents for item in protected))


def _shell_path_risk(word: str, *, cwd: str, worktree_root: str, home: str) -> str:
    value = word.split("=", 1)[-1] if "=" in word else word
    pattern = _path_pattern(value, cwd=cwd, home=home)
    protected = (
        Path(home).resolve() / ".cache" / "macos-cua" / "typed-approve.token",
        Path(home).resolve() / ".cache" / "macos-cua" / "samantha-activity.jsonl",
    )
    wildcard = min((value.find(ch) for ch in "*?[" if ch in value), default=-1)
    if wildcard < 0:
        expanded = value.replace("${HOME}", home).replace("$HOME", home)
        if "$" in expanded or "`" in value:
            return "approval"
        try:
            resolved = str(Path(pattern).resolve(strict=False))
        except (OSError, RuntimeError):
            return "approval"
        return "hard_refuse" if _is_protected_path(resolved, home=home) else "safe"
    else:
        if re.search(r"(?i)typed-approve|samantha-activity|\.env|config/\.secret|\.pem|id_", value):
            return "hard_refuse"
        expanded = value.replace("${HOME}", home).replace("$HOME", home)
        if "$" in expanded or "`" in expanded:
            return "approval"
        wildcard = min((pattern.find(ch) for ch in "*?[" if ch in pattern), default=-1)
        prefix = pattern[:wildcard]
        if not prefix.endswith(os.sep):
            prefix = prefix[:prefix.rfind(os.sep)] or os.sep
        else:
            prefix = prefix.rstrip(os.sep) or os.sep
        try:
            resolved_prefix = str(Path(prefix).resolve(strict=False))
        except (OSError, RuntimeError):
            return "approval"
        if _touches_protected_path(resolved_prefix, home=home):
            return "hard_refuse"
        resolved_pattern = os.path.join(resolved_prefix, pattern[len(prefix):].lstrip(os.sep))
        if any(fnmatch.fnmatchcase(str(path), resolved_pattern) for path in protected):
            return "hard_refuse"
        if not _inside(resolved_prefix, os.path.realpath(worktree_root)):
            return "approval"
        try:
            matches = glob.glob(pattern, recursive=True)
        except (OSError, ValueError):
            return "approval"
        for match in matches:
            try:
                resolved_match = str(Path(match).resolve(strict=False))
            except (OSError, RuntimeError):
                return "approval"
            if _touches_protected_path(resolved_match, home=home):
                return "hard_refuse"
            if not _inside(resolved_match, os.path.realpath(worktree_root)):
                return "approval"
        return "safe"


def _recursive_rm_root(words: list[str], *, cwd: str, worktree_root: str, home: str) -> bool:
    if not words or os.path.basename(words[0]) != "rm":
        return False
    recursive = False
    targets: list[str] = []
    options = True
    for word in words[1:]:
        if options and word == "--":
            options = False
        elif options and word.startswith("-"):
            recursive |= word == "--recursive" or (not word.startswith("--") and bool(set(word[1:]) & {"r", "R"}))
        else:
            options = False
            targets.append(word)
    if not recursive:
        return False
    roots = {"/", os.path.realpath(home), os.path.realpath(worktree_root)}
    for target in targets:
        if re.match(r"^\$(?:HOME|\{HOME\})(?:/|$)", target):
            expanded = target.replace("${HOME}", home).replace("$HOME", home)
            try:
                path = str(Path(expanded).resolve(strict=False))
            except (OSError, RuntimeError):
                path = None
        else:
            path = _resolved_path(target, cwd=cwd, home=home)
        if path in roots:
            return True
    return False


def _has_control_syntax(segments: list[str]) -> bool:
    for segment in segments:
        words = _words(segment)
        effective, _ = _unwrap_command(words)
        words = effective or words
        xargs_replacements: set[int] = set()
        if words and os.path.basename(words[0]) == "xargs":
            for i, word in enumerate(words[1:], 1):
                if word in {"-I", "--replace"} and i + 1 < len(words):
                    xargs_replacements.add(i + 1)
                elif word.startswith("-I") and len(word) > 2 or word.startswith("--replace="):
                    xargs_replacements.add(i)
        if words and (words[0] in _SHELL_CONTROL or any(
            i not in xargs_replacements and any(ch in word for ch in "{}()")
            for i, word in enumerate(words)
        )):
            return True
    return False


def _redirect_targets(segment: str) -> list[str]:
    targets: list[str] = []
    quote = ""
    escaped = False
    i = 0
    while i < len(segment):
        ch = segment[i]
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in {"'", '"'}:
            quote = ch
        elif ch == ">":
            if i + 1 < len(segment) and segment[i + 1] == ">":
                i += 1
            i += 1
            while i < len(segment) and segment[i].isspace():
                i += 1
            if i < len(segment) and segment[i] == "&":
                while i < len(segment) and not segment[i].isspace():
                    i += 1
                continue
            if i >= len(segment):
                targets.append("")
                break
            if segment[i] in {"'", '"'}:
                q = segment[i]
                i += 1
                start = i
                while i < len(segment) and segment[i] != q:
                    i += 1
                targets.append(segment[start:i])
                continue
            start = i
            while i < len(segment) and not segment[i].isspace() and segment[i] not in ";|&":
                i += 1
            targets.append(segment[start:i])
            continue
        i += 1
    return targets


def _inline_read_only(words: list[str], *, flag: str, language: str) -> bool:
    try:
        code = words[next(i for i, word in enumerate(words[1:], 1) if word == flag or word.startswith(flag)) + 1]
    except (StopIteration, IndexError):
        return False
    if language == "node":
        return bool(re.fullmatch(r'''console\.log\((?:[0-9\s+*/().,-]|'[^'\\]*'|"[^"\\]*")*\);?''', code))
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError:
        return False
    allowed = (ast.Expression, ast.Call, ast.Name, ast.Constant, ast.BinOp, ast.UnaryOp,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
               ast.USub, ast.UAdd, ast.Load)
    return all(isinstance(node, allowed) for node in ast.walk(tree)) and all(
        not isinstance(node, ast.Call) or isinstance(node.func, ast.Name) and node.func.id == "print"
        for node in ast.walk(tree))


def _git_subcommand(words: list[str]) -> str:
    i = 1
    takes_value = {"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
    while i < len(words):
        word = words[i]
        if word in takes_value:
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            return word
    return ""


def _unwrap_command(words: list[str]) -> tuple[list[str], str]:
    wrappers = {"builtin", "caffeinate", "command", "env", "exec", "gtimeout", "nice", "nohup",
                "stdbuf", "time", "timeout", "xargs"}
    current = words
    while current:
        name = os.path.basename(current[0])
        if name == "sudo" or name not in wrappers:
            return current, ""
        args = current[1:]
        i = 0
        if name == "env":
            while i < len(args) and args[i].startswith("-"):
                option = args[i]
                if option == "--":
                    i += 1
                    break
                if option in {"-i", "--ignore-environment"}:
                    i += 1
                elif option in {"-u", "--unset"}:
                    if i + 1 >= len(args):
                        return current, "env wrapper is missing an option value"
                    i += 2
                elif ((option.startswith("-u") and len(option) > 2)
                      or (option.startswith("--unset=") and len(option) > len("--unset="))):
                    i += 1
                else:
                    return current, "unsupported env wrapper option"
            while i < len(args) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", args[i]):
                if "$" in args[i] or "`" in args[i]:
                    return current, "dynamic env assignment"
                i += 1
        elif name in {"timeout", "gtimeout"}:
            while i < len(args) and args[i].startswith("-"):
                option = args[i]
                if option == "--":
                    i += 1
                    break
                if option in {"--foreground", "--preserve-status", "--verbose", "-v"}:
                    i += 1
                elif option in {"-k", "--kill-after", "-s", "--signal"}:
                    if i + 1 >= len(args):
                        return current, "timeout wrapper is missing an option value"
                    i += 2
                elif option.startswith("--kill-after=") or option.startswith("--signal="):
                    i += 1
                else:
                    return current, "unsupported timeout wrapper option"
            if i >= len(args):
                return current, "timeout wrapper is missing its duration and command"
            i += 1
        elif name == "xargs":
            value_options = {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s",
                             "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines",
                             "--max-args", "--max-procs", "--max-chars"}
            flags = {"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose", "-x", "--exit",
                     "--show-limits"}
            while i < len(args) and args[i].startswith("-"):
                option = args[i]
                if option == "--":
                    i += 1
                    break
                if option in flags:
                    i += 1
                elif option in value_options:
                    if i + 1 >= len(args):
                        return current, "xargs wrapper is missing an option value"
                    i += 2
                elif any(option.startswith(value + "=") for value in value_options if value.startswith("--")):
                    i += 1
                elif any(option.startswith(value) and option != value for value in ("-I", "-L", "-n", "-P", "-s")):
                    i += 1
                else:
                    return current, "unsupported xargs wrapper option"
        else:
            while i < len(args) and args[i].startswith("-"):
                option = args[i]
                if option == "--":
                    i += 1
                    break
                if name == "command" and option in {"-p", "-v", "-V"}:
                    i += 1
                elif name == "builtin":
                    return current, "unsupported builtin wrapper option"
                elif name == "exec" and option in {"-c", "-l", "-p"}:
                    i += 1
                elif name == "exec" and option == "-a":
                    if i + 1 >= len(args):
                        return current, "exec wrapper is missing an option value"
                    i += 2
                elif name == "nice" and option in {"-n", "--adjustment"}:
                    if i + 1 >= len(args):
                        return current, "nice wrapper is missing an option value"
                    i += 2
                elif name == "nice" and (re.fullmatch(r"-n[+-]?\d+", option)
                                          or re.fullmatch(r"-[+-]?\d+", option)
                                          or (option.startswith("--adjustment=")
                                              and len(option) > len("--adjustment="))):
                    i += 1
                elif name == "time" and option == "-p":
                    i += 1
                elif name == "caffeinate" and re.fullmatch(r"-[dismu]+", option):
                    i += 1
                elif name == "caffeinate" and option in {"-t", "-w"}:
                    if i + 1 >= len(args):
                        return current, "caffeinate wrapper is missing an option value"
                    i += 2
                elif name == "caffeinate" and re.fullmatch(r"-[tw].+", option):
                    i += 1
                elif name == "stdbuf" and option in {"-i", "-o", "-e", "--input", "--output", "--error"}:
                    if i + 1 >= len(args):
                        return current, "stdbuf wrapper is missing an option value"
                    i += 2
                elif name == "stdbuf" and (option.startswith("--input=") or option.startswith("--output=")
                                            or option.startswith("--error=") or re.fullmatch(r"-[ioe].+", option)):
                    i += 1
                elif name == "nohup":
                    return current, "unsupported nohup wrapper option"
                else:
                    return current, f"unsupported {name} wrapper option"
        if i >= len(args):
            return current, f"{name} wrapper is missing a command"
        current = args[i:]
    return current, "wrapper is missing a command"


def classify_shell_command(
    command: str, *, cwd: str, worktree_root: str, home: str,
) -> tuple[str, str]:
    """Pure shell risk classifier: returns (safe|approval|hard_refuse, reason)."""
    if any(re.search(rf"(?<!\d){port}(?!\d)", command) for port in TURN_PORTS):
        return "hard_refuse", "protected gateway port"
    # This guardrail cannot stop a repository script from calling localhost directly.
    if re.search(r"(?i)api/(?:confirm|session/approve)|typed-approve", command) or re.search(
        r"(?i)\bhttps?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/|(?=\s|$))", command,
    ):
        return "hard_refuse", "protected confirmation endpoint or token file"
    if re.search(r"(?i)\.cache/macos-cua/samantha-activity\.jsonl", command):
        return "hard_refuse", "protected activity log"
    if re.search(r"(?i)\bsudo\b", command):
        return "hard_refuse", "sudo is refused"
    if re.search(r"(?i)\bgit\s+push\b[^;\n|]*?(?:--force(?:-with-lease)?|(?<!\S)-f(?=\s|$))", command):
        return "hard_refuse", "force push is refused"
    if _SECRET_PATH.search(command) or re.search(r"(?i)(?:^|[\s;&|])security(?:\s|$)", command):
        return "hard_refuse", "secret file or Keychain access"

    segments = _shell_segments(command)
    control_syntax = _has_control_syntax(segments)
    if len(segments) == 1:
        inline_words = _words(segments[0])
        inline, _ = _unwrap_command(inline_words)
        if inline:
            inline_name = os.path.basename(inline[0])
            if ((inline_name in {"python", "python3", "python2", "pypy", "pypy3"}
                 and any(word.startswith("-c") for word in inline[1:])
                 and _inline_read_only(inline, flag="-c", language="python"))
                    or (inline_name == "node" and any(word.startswith("-e") for word in inline[1:])
                        and _inline_read_only(inline, flag="-e", language="node"))):
                control_syntax = False
    wrapper_errors: list[str] = []
    path_approval = False
    for segment in segments:
        words = _words(segment)
        if not words:
            wrapper_errors.append("shell quoting could not be parsed")
            continue
        if any(os.path.basename(word) == "security" or _SECRET_PATH.search(word) for word in words):
            return "hard_refuse", "secret file or Keychain access"
        path_risks = [_shell_path_risk(word, cwd=cwd, worktree_root=worktree_root, home=home) for word in words]
        if "hard_refuse" in path_risks:
            return "hard_refuse", "protected secret path"
        path_approval |= "approval" in path_risks
        if any(os.path.basename(word) == "sudo" for word in words):
            return "hard_refuse", "sudo is refused"
        effective, wrapper_error = _unwrap_command(words)
        if wrapper_error:
            wrapper_errors.append(wrapper_error)
        if effective and os.path.basename(effective[0]) == "git" and _git_subcommand(effective) == "push":
            if any(word in {"--force", "--force-with-lease"} or
                   re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", word) for word in effective[1:]):
                return "hard_refuse", "force push is refused"
        candidates = [effective]
        if control_syntax or wrapper_error:
            candidates.extend(words[i:] for i, word in enumerate(words) if os.path.basename(word) == "rm")
            candidates.extend(words[i:] for i, word in enumerate(words) if os.path.basename(word) == "git")
        if any(_recursive_rm_root(candidate, cwd=cwd, worktree_root=worktree_root, home=home)
               for candidate in candidates):
            return "hard_refuse", "recursive removal of a protected root"
        for candidate in candidates:
            if candidate and os.path.basename(candidate[0]) == "git" and _git_subcommand(candidate) == "push":
                if any(word in {"--force", "--force-with-lease"} or
                       re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", word) for word in candidate[1:]):
                    return "hard_refuse", "force push is refused"

    approval = ""
    if control_syntax:
        approval = "unsupported shell control syntax"
    elif wrapper_errors:
        approval = wrapper_errors[0]
    elif path_approval:
        approval = "glob path outside the worktree or ambiguous"
    elif "\n" in command or "\r" in command:
        approval = "multiline shell command"
    elif "$(" in command or "`" in command or re.search(r"[<>]\(", command):
        approval = "command substitution"
    elif re.search(r"\b(?:curl|wget)\b[^;\n]*\|\s*(?:sh|bash|zsh)(?:\s|$)", command):
        approval = "download piped to a shell"
    else:
        for segment in segments:
            words = _words(segment)
            if not words:
                continue
            head = words[0]
            effective, wrapper_error = _unwrap_command(words)
            if wrapper_error:
                approval = wrapper_error
                break
            if not effective:
                continue
            executable = effective[0]
            name = os.path.basename(executable)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", head) or "$" in head:
                approval = "variable in command head"
                break
            if name in {"eval", "source", "."}:
                approval = "dynamic shell evaluation"
                break
            if name in {"sh", "bash", "zsh"} and any(
                re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word) for word in effective[1:]
            ):
                approval = "nested shell"
                break
            if name in {"python", "python3", "python2", "pypy", "pypy3"} and any(
                re.fullmatch(r"-c[A-Za-z]*", word) for word in effective[1:]
            ) and not _inline_read_only(effective, flag="-c", language="python"):
                approval = "inline Python may modify files"
                break
            if name in {"python", "python3", "python2", "pypy", "pypy3"} and any(
                effective[i:i + 2] == ["-m", "pip"] and effective[i + 2:i + 3] == ["install"]
                for i in range(1, len(effective) - 1)
            ):
                approval = "package install"
                break
            if name == "node" and any(re.fullmatch(r"-e[A-Za-z]*", word) for word in effective[1:]) and not (
                _inline_read_only(effective, flag="-e", language="node")
            ):
                approval = "inline Node may modify files"
                break
            if executable.startswith("/"):
                parent = os.path.dirname(os.path.normpath(executable))
                if parent not in _STANDARD_BIN_DIRS:
                    approval = "absolute executable outside standard bin directories"
                    break
            if name == "git":
                if any(word == "-c" or word.startswith("-c") for word in effective[1:]):
                    approval = "git config override"
                    break
                subcommand = _git_subcommand(effective)
                if subcommand in {"commit", "push", "rebase", "clean"}:
                    approval = f"git {subcommand}"
                    break
                if subcommand == "reset" and "--hard" in effective:
                    approval = "git reset --hard"
                    break
                if subcommand == "checkout" and "--" in effective:
                    if any(word in {".", "./"} for word in effective[effective.index("--") + 1:]):
                        approval = "git checkout -- ."
                        break
                if subcommand == "branch" and any(word in {"-d", "-D", "--delete"} for word in effective):
                    approval = "git branch delete"
                    break
                if subcommand == "config" and any(word.startswith("alias.") for word in effective):
                    approval = "git alias configuration"
                    break
                if subcommand and subcommand not in {
                    "add", "status", "diff", "log", "branch", "show", "rev-parse", "ls-files",
                    "cat-file", "remote", "tag", "fetch", "merge-base", "worktree", "grep",
                    "blame", "help", "version", "config",
                }:
                    approval = "unknown git subcommand or alias"
                    break
            if name in {"rm", "mv", "chmod", "kill", "pkill", "killall", "launchctl"}:
                approval = name
                break
            if name == "find" and any(word in {"-exec", "-execdir", "-ok", "-okdir", "-delete"}
                                       for word in effective[1:]):
                approval = "find can modify files or execute commands"
                break
            if name == "brew" and any(word == "install" for word in effective[1:]):
                approval = "brew install"
                break
            if name in {"npm", "npx"} and any(word in {"install", "i", "add", "publish"} for word in effective[1:]):
                approval = "npm install or publish"
                break
            if name in {"pip", "pip3", "uv"} and any(word in {"install", "add"} for word in effective[1:]):
                approval = "package install"
                break
            if name in {"twine", "docker"} and any(word in {"upload", "push"} for word in effective[1:]):
                approval = "network publish"
                break
            if name == "gh" and any(word in {"release", "publish", "create", "merge", "close", "edit"}
                                     for word in effective[1:]) and any(word in {"pr", "release"} for word in effective[1:]):
                approval = "network publish"
                break
            for target in _redirect_targets(segment):
                if target == "/dev/null":
                    continue
                resolved = _resolved_path(target, cwd=cwd, home=home)
                if resolved is None or not _inside(resolved, os.path.realpath(worktree_root)):
                    approval = "redirect outside the worktree"
                    break
            if approval:
                break
    return ("approval", approval) if approval else ("safe", "")


def _scrubbed_env() -> dict[str, str]:
    # GIT_CONFIG_COUNT/KEY_n/VALUE_n are one set: dropping KEY_n alone (it contains "KEY") breaks every git call.
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith("GIT_CONFIG_")
            and not any(part in key.upper() for part in _SECRET_ENV_PARTS)}


def _workspace_roots() -> tuple[Path, Path]:
    home = Path.home().resolve()
    return home / "code", home / "Documents" / "remote-claude"


def _git_root(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(["/usr/bin/git", "rev-parse", "--show-toplevel"], cwd=cwd,
                                env=_scrubbed_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve(strict=True)
    except OSError:
        return None


def _resolve_cwd(value: Any) -> tuple[Path, Path] | dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {"ok": False, "error": "cwd must be an absolute or ~-based path", "error_type": "invalid_cwd"}
    raw = value.strip()
    if not (raw.startswith("~") or Path(raw).is_absolute()):
        return {"ok": False, "error": "cwd must be an absolute or ~-based path", "error_type": "invalid_cwd"}
    try:
        cwd = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return {"ok": False, "error": "cwd does not exist", "error_type": "invalid_cwd"}
    if not cwd.is_dir():
        return {"ok": False, "error": "cwd must be a directory", "error_type": "invalid_cwd"}
    root = _git_root(cwd)
    if root is None or not _inside(str(cwd), str(root)):
        return {"ok": False, "error": "cwd must be inside a git worktree", "error_type": "invalid_cwd"}
    if not any(_inside(str(root), str(allowed.resolve())) for allowed in _workspace_roots()):
        return {"ok": False, "error": "git worktree must be under ~/code or ~/Documents/remote-claude",
                "error_type": "invalid_cwd"}
    return cwd, root


def _command_head(command: str) -> str:
    words = _words(_shell_segments(command)[0]) if _shell_segments(command) else []
    return os.path.basename(words[0])[:80] if words else ""


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()


def _hard_refusal(reason: str) -> dict[str, Any]:
    return {"ok": False, "error": f"refused: {reason}", "error_type": "hard_refusal",
            "retryable": False}


def _approval(tool: str, args: dict[str, Any], description: str) -> dict[str, Any] | None:
    try:
        result = GATE.check(tool, args, description)
    except Exception:  # noqa: BLE001 — approval failures must never authorize execution
        return {"ok": False, "error": "approval gate failed closed", "error_type": "approval_gate_error",
                "retryable": False}
    if result is None or isinstance(result, dict):
        return result
    return {"ok": False, "error": "approval gate returned an invalid result",
            "error_type": "approval_gate_error", "retryable": False}


def _protected_listener_pids() -> set[int] | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-t", "-iTCP:8765", "-iTCP:8787", "-sTCP:LISTEN"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3,
            env=_scrubbed_env(), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in {0, 1}:
        return None
    try:
        return {int(line) for line in result.stdout.splitlines() if line.strip()}
    except ValueError:
        return None


def _group_is_protected(pgid: int) -> bool | None:
    pids = _protected_listener_pids()
    if pids is None:
        return None
    for pid in pids:
        try:
            if os.getpgid(pid) == pgid:
                return True
        except ProcessLookupError:
            continue
        except OSError:
            return None
    return False


def _terminate_group(proc: subprocess.Popen[Any], *, grace_s: float = 1.0) -> tuple[bool, str]:
    protected = _group_is_protected(proc.pid)
    if protected is None:
        return False, "could not verify protected gateway ports"
    if protected:
        return False, "process group owns a protected gateway port"
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, ""
    except OSError as exc:
        return False, type(exc).__name__
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, 0)
        group_remains = True
    except ProcessLookupError:
        group_remains = False
    except OSError:
        return False, "could not inspect process group"
    if group_remains:
        protected = _group_is_protected(proc.pid)
        if protected is None or protected:
            return False, "protected gateway process check failed"
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True, ""
        except OSError as exc:
            return False, type(exc).__name__
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            return False, "process group did not exit"
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            pass
        except OSError:
            return False, "could not inspect process group"
        else:
            return False, "process group did not exit"
    return True, ""


def _process_targets(command: str) -> set[int] | None:
    targets: set[int] = set()
    controlled = {"kill", "pkill", "killall", "launchctl"}
    for segment in _shell_segments(command):
        words = _words(segment)
        if not words:
            continue
        effective, wrapper_error = _unwrap_command(words)
        if wrapper_error:
            if any(os.path.basename(word) in controlled for word in words):
                return None
            continue
        if not effective:
            continue
        name = os.path.basename(effective[0])
        if name == "launchctl":
            if any(word in {"kill", "bootout", "unload", "stop"} for word in effective[1:]) or (
                "kickstart" in effective and "-k" in effective
            ):
                return None
            continue
        if name == "kill":
            options = True
            for word in effective[1:]:
                if options and word == "--":
                    options = False
                    continue
                if options and word.startswith("-"):
                    continue
                try:
                    pid = int(word)
                except ValueError:
                    return None
                if pid <= 0:
                    return None
                targets.add(pid)
            continue
        if name in {"pkill", "killall"}:
            options = [word for word in effective[1:] if word.startswith("-")]
            if any(option not in {"-x", "-TERM", "-KILL", "-INT", "-HUP", "-9", "-15", "-2", "-1"}
                   for option in options):
                return None
            patterns = [word for word in effective[1:] if not word.startswith("-")]
            if len(patterns) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+", patterns[0]):
                return None
            try:
                result = subprocess.run(["/usr/bin/pgrep", "-x", patterns[0]],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, timeout=3, check=False, env=_scrubbed_env())
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode == 0:
                try:
                    targets.update(int(line) for line in result.stdout.splitlines() if line.strip())
                except ValueError:
                    return None
            elif result.returncode != 1:
                return None
    return targets


def _protect_process_control(command: str) -> dict[str, Any] | None:
    controlled = {"kill", "pkill", "killall", "launchctl"}
    names: set[str] = set()
    for segment in _shell_segments(command):
        words = _words(segment)
        if not words:
            continue
        effective, wrapper_error = _unwrap_command(words)
        if wrapper_error and any(os.path.basename(word) in controlled for word in words):
            return _hard_refusal("process target cannot be verified safely")
        if effective:
            names.add(os.path.basename(effective[0]))
    if not names.intersection({"kill", "pkill", "killall", "launchctl"}):
        return None
    if "launchctl" in names and _process_targets(command) is None:
        return _hard_refusal("launchctl process termination cannot be verified safely")
    targets = _process_targets(command)
    if targets is None:
        return _hard_refusal("process target cannot be verified safely")
    protected = _protected_listener_pids()
    if protected is None:
        return _hard_refusal("could not verify protected gateway ports")
    if targets.intersection(protected):
        return _hard_refusal("target process owns a protected gateway port")
    return None


def _tail_stream(stream: Any, limit: int = OUTPUT_CHARS) -> tuple[str, bool, int]:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    total_bytes = stream.tell()
    stream.seek(max(0, total_bytes - limit))
    raw = stream.read(limit)
    output = raw.decode("utf-8", errors="replace")[-limit:]
    return output, total_bytes > limit, total_bytes


def _run_command(command: str, cwd: Path, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.Popen(
                ["/bin/zsh", "-lc", "source /dev/stdin"], cwd=cwd, stdin=subprocess.PIPE,
                stdout=output, stderr=subprocess.STDOUT, env=_scrubbed_env(), start_new_session=True,
            )
            timed_out = False
            termination_error = ""
            try:
                proc.communicate(input=command.encode("utf-8"), timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminated, termination_error = _terminate_group(proc)
                if terminated:
                    try:
                        proc.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        termination_error = "process group did not exit"
            output_text, truncated, total_bytes = _tail_stream(output)
            return {
                "ok": not timed_out and proc.returncode == 0,
                "exit_code": proc.poll(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output": output_text,
                "truncated": truncated,
                "total_bytes": total_bytes,
                "timed_out": timed_out,
                **({"termination_error": termination_error} if termination_error else {}),
            }
        except OSError as exc:
            return {"ok": False, "error": f"could not start shell: {type(exc).__name__}",
                    "error_type": "shell_start_failed", "duration_ms": int((time.monotonic() - started) * 1000),
                    "exit_code": None, "output": "", "truncated": False, "total_bytes": 0,
                    "timed_out": False}


def _shell_run(args: dict[str, Any]) -> dict[str, Any]:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"ok": False, "error": "command is required", "error_type": "invalid_command"}
    timeout = args.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return {"ok": False, "error": "timeout_s must be finite", "error_type": "invalid_timeout"}
    try:
        timeout = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "timeout_s must be finite", "error_type": "invalid_timeout"}
    if not math.isfinite(timeout):
        return {"ok": False, "error": "timeout_s must be finite", "error_type": "invalid_timeout"}
    if timeout <= 0 or timeout > MAX_TIMEOUT_S:
        return {"ok": False, "error": "timeout_s must be greater than zero and at most 900", "error_type": "invalid_timeout"}
    background = args.get("background", False)
    if not isinstance(background, bool):
        return {"ok": False, "error": "background must be a boolean", "error_type": "invalid_argument"}
    resolved = _resolve_cwd(args.get("cwd"))
    if isinstance(resolved, dict):
        return resolved
    cwd, root = resolved
    decision, reason = classify_shell_command(command, cwd=str(cwd), worktree_root=str(root), home=str(Path.home()))
    if decision == "hard_refuse":
        return _hard_refusal(reason)
    if decision == "approval":
        denied = _approval("shell_run", args, f"Allow {command[:120]} in {root.name}?")
        if denied is not None:
            return denied
    process_refusal = _protect_process_control(command)
    if process_refusal is not None:
        return process_refusal
    if background:
        return _start_background(command, cwd)
    return _run_command(command, cwd, timeout)


def _jobs_dir() -> Path:
    return Path.home() / ".cache" / "macos-cua" / "shell-jobs"


def _start_background(command: str, cwd: Path) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    directory = _jobs_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        path = directory / f"{job_id}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
        log = os.fdopen(fd, "wb")
    except OSError as exc:
        return {"ok": False, "error": f"could not create job log: {type(exc).__name__}",
                "error_type": "job_log_failed"}
    try:
        proc = subprocess.Popen(
            ["/bin/zsh", "-lc", "source /dev/stdin"], cwd=cwd, stdin=subprocess.PIPE,
            stdout=log, stderr=subprocess.STDOUT, env=_scrubbed_env(), start_new_session=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(command.encode("utf-8"))
        proc.stdin.close()
    except OSError as exc:
        if "proc" in locals():
            _terminate_group(proc)
        log.close()
        try:
            path.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"could not start shell job: {type(exc).__name__}",
                "error_type": "shell_start_failed"}
    log.close()
    with _JOB_LOCK:
        _JOBS[job_id] = {"process": proc, "path": path, "started_at": time.monotonic(),
                         "command_hash": _command_hash(command), "command_head": _command_head(command)}
    # A successful launch only proves the process started, not that its task completed.
    return {"ok": True, "background": True, "job_id": job_id,
            "pid": proc.pid, "log": str(path)}


def _job(job_id: Any) -> dict[str, Any] | None:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return None
    with _JOB_LOCK:
        return _JOBS.get(job_id)


def shell_job(args: dict[str, Any]) -> dict[str, Any]:
    job_id = args.get("job_id")
    job = _job(job_id)
    if job is None:
        return {"ok": False, "error": "unknown job_id", "error_type": "unknown_job"}
    action = args.get("action")
    if action not in {"status", "tail", "stop"}:
        return {"ok": False, "error": "action must be status, tail, or stop", "error_type": "invalid_argument"}
    proc = job["process"]
    if action == "stop":
        stopped, reason = _terminate_group(proc)
        if not stopped:
            return {"ok": False, "error": reason, "error_type": "stop_refused"}
    exit_code = proc.poll()
    status = "running" if exit_code is None else "done"
    result: dict[str, Any] = {"ok": True, "job_id": job_id, "status": status,
                              "exit_code": exit_code, "duration_ms": int((time.monotonic() - job["started_at"]) * 1000),
                              "command_hash": job["command_hash"], "command_head": job["command_head"]}
    if action == "tail":
        try:
            with open(job["path"], "rb") as f:
                f.seek(0, os.SEEK_END)
                total = f.tell()
                f.seek(max(0, total - OUTPUT_CHARS))
                raw = f.read(OUTPUT_CHARS)
            result.update(output=raw.decode("utf-8", errors="replace")[-OUTPUT_CHARS:],
                          truncated=total > OUTPUT_CHARS, total_bytes=total)
        except OSError as exc:
            return {"ok": False, "error": f"could not read job log: {type(exc).__name__}",
                    "error_type": "job_log_failed"}
    if status == "done":
        result["job_done"] = True
        if exit_code == 0:
            result["outcome"] = "verified"
        else:
            result["outcome"] = "unknown"
    if action == "stop" and exit_code is not None:
        result["stopped"] = exit_code != 0
    return result


def shutdown_jobs() -> None:
    with _JOB_LOCK:
        jobs = list(_JOBS.values())
    for job in jobs:
        proc = job["process"]
        _terminate_group(proc)
    with _JOB_LOCK:
        _JOBS.clear()


def _lexical_git_root(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def _file_target(raw: Any) -> tuple[Path, Path | None] | dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {"ok": False, "error": "path is required", "error_type": "invalid_path"}
    value = raw.strip()
    if not (value.startswith("~") or Path(value).is_absolute()):
        return {"ok": False, "error": "path must be absolute or ~-based", "error_type": "invalid_path"}
    lexical = Path(value).expanduser()
    try:
        lexical = Path(os.path.abspath(lexical))
        root_lexical = _lexical_git_root(lexical)
        if lexical.exists() or lexical.is_symlink():
            target = lexical.resolve(strict=True)
            if target.exists():
                mode = target.stat().st_mode
                if not stat.S_ISREG(mode):
                    return {"ok": False, "error": "special files and directories are refused", "error_type": "invalid_path"}
        else:
            parent = lexical.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            parent_real = parent.resolve(strict=True)
            target = parent_real.joinpath(*lexical.parent.relative_to(parent).parts, lexical.name)
        root = root_lexical.resolve(strict=True) if root_lexical else None
        if root is not None:
            try:
                lexical.relative_to(root_lexical)
            except ValueError:
                pass
            if not _inside(str(target), str(root)):
                return {"ok": False, "error": "symlink escapes its git worktree", "error_type": "path_escape"}
            relative = lexical.relative_to(root_lexical)
            if ".git" in relative.parts or ".git" in target.relative_to(root).parts:
                return {"ok": False, "error": "writes into .git are refused", "error_type": "protected_path"}
        if _SECRET_PATH.search(str(lexical)) or _SECRET_PATH.search(str(target)):
            return {"ok": False, "error": "secret files are refused", "error_type": "protected_path"}
        home = Path.home().resolve()
        if (lexical == home / ".cache" / "macos-cua" / "samantha-activity.jsonl"
                or target == home / ".cache" / "macos-cua" / "samantha-activity.jsonl"):
            return {"ok": False, "error": "the activity log is protected", "error_type": "protected_path"}
        if lexical.name == "typed-approve.token" or target.name == "typed-approve.token":
            return {"ok": False, "error": "the typed approval token is protected", "error_type": "protected_path"}
        if re.search(r"(?i)(?:^|/)Library/Keychains(?:/|$)", str(lexical)):
            return {"ok": False, "error": "Keychain files are refused", "error_type": "protected_path"}
        return target, root
    except (OSError, RuntimeError, ValueError):
        return {"ok": False, "error": "path could not be resolved safely", "error_type": "invalid_path"}


def _file_gate(op: str, path: Path, root: Path | None, args: dict[str, Any]) -> dict[str, Any] | None:
    if op in {"write", "patch"} and root is None:
        return _approval("file_edit", args, f"Allow {op} to {path.name} outside a git worktree?")
    return None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temp_name = tempfile.mkstemp(prefix=".file-edit-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def file_edit(args: dict[str, Any]) -> dict[str, Any]:
    op = args.get("op")
    if op not in {"read", "write", "patch"}:
        return {"ok": False, "error": "op must be read, write, or patch", "error_type": "invalid_argument"}
    resolved = _file_target(args.get("path"))
    if isinstance(resolved, dict):
        return resolved
    path, root = resolved
    if op == "read":
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "error": f"could not read file: {type(exc).__name__}", "error_type": "read_failed"}
        offset = args.get("offset", 0)
        limit = args.get("limit", 100)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return {"ok": False, "error": "offset and limit must be non-negative/positive integers", "error_type": "invalid_argument"}
        selected = lines[offset:offset + min(limit, 1000)]
        numbered: list[str] = []
        chars = 0
        char_truncated = False
        for index, line in enumerate(selected, offset + 1):
            row = f"{index:>5}: {line}"
            room = FILE_CHARS - chars - 1
            if room <= 0:
                char_truncated = True
                break
            if len(row) > room:
                numbered.append(row[:room])
                char_truncated = True
                break
            numbered.append(row)
            chars += len(row) + 1
        content = "\n".join(numbered)
        return {"ok": True, "path": path.name, "content": content, "offset": offset,
                "lines": len(numbered), "total_lines": len(lines),
                "truncated": char_truncated or offset + len(numbered) < len(lines)}
    if op == "write":
        content = args.get("content")
        if not isinstance(content, str):
            return {"ok": False, "error": "content is required", "error_type": "invalid_argument"}
        denied = _file_gate(op, path, root, args)
        if denied is not None:
            return denied
        try:
            _atomic_write(path, content)
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "error": f"could not write file: {type(exc).__name__}", "error_type": "write_failed"}
        return {"ok": True, "path": path.name, "bytes": len(content.encode("utf-8")),
                "lines": len(content.splitlines())}
    old, new = args.get("old"), args.get("new")
    replace_all = args.get("replace_all", False)
    if not isinstance(old, str) or not old or not isinstance(new, str) or not isinstance(replace_all, bool):
        return {"ok": False, "error": "patch requires non-empty old, new, and boolean replace_all",
                "error_type": "invalid_argument"}
    denied = _file_gate(op, path, root, args)
    if denied is not None:
        return denied
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {"ok": False, "error": f"could not read patch target: {type(exc).__name__}", "error_type": "read_failed"}
    matches = content.count(old)
    if matches != 1 and not (replace_all and matches > 0):
        return {"ok": False, "error": f"old text must match exactly once (found {matches})",
                "error_type": "patch_match_count", "matches": matches}
    updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    try:
        _atomic_write(path, updated)
    except OSError as exc:
        return {"ok": False, "error": f"could not write file: {type(exc).__name__}", "error_type": "write_failed"}
    return {"ok": True, "path": path.name, "replacements": matches if replace_all else 1,
            "added_lines": max(0, len(updated.splitlines()) - len(content.splitlines())),
            "removed_lines": max(0, len(content.splitlines()) - len(updated.splitlines()))}


def shell_run(args: dict[str, Any]) -> dict[str, Any]:
    return _shell_run(args)
