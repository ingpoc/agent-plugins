"""Plain-language, rule-based text for Kindle findings (no LLM) and its privacy bounds.

Evidence excerpts (the command that ran, the first error line, the failing test) come
from Codex rollout output. They are redacted here before they are stored or shipped:
one line only, home paths shortened to ``~``, temp paths and token-like strings
replaced, and at most ``MAX_EXCERPT`` characters. Rules only restate the evidence; when
a rule has no evidence, the field is ``"unknown"`` and the Kindle shows nothing.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime
from pathlib import Path

UNKNOWN = "unknown"
MAX_EXCERPT = 120
MAX_FIX = 160
STEP_LABELS = {("wf:build-quality", "step:unit-discovery"): "running the test suite"}
# Staleness rule (contract schema 4, deterministic, no LLM; Guru 20 Oct 18:50): an
# unrecovered finding is "stale" (possibly stale, unconfirmed; never "fixed") when no
# new failure came for STALE_ALWAYS_AFTER_S (any run count), or when it failed in at
# most STALE_MAX_FAILED_RUNS run and nothing came for STALE_AFTER_S (a one-off loop).
STALE_AFTER_S = 2 * 3600
STALE_MAX_FAILED_RUNS = 1
STALE_ALWAYS_AFTER_S = 24 * 3600

_HOME = str(Path.home())
_SECRET_PAIR = re.compile(r"(?i)\b(bearer|token|passw(?:or)?d|secret|api[_-]?key|authorization|cookie)(\s*[=:]\s*|\s+)"
                          r"(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|\S+)")
_KNOWN_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[abpr]-[A-Za-z0-9-]{8,}"
                          r"|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+)")
_LONG_RUN = re.compile(r"[A-Za-z0-9+=]{24,}")
_HEX = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_TMP = re.compile(r"(?:/private)?/var/folders/[^\s'\"]*|/tmp/[^\s'\"]+")
_USERS = re.compile(r"/Users/[^/\s'\"]+")
# A path of 3+ parts (absolute, ~/ or relative); long lines shed its leading parts first.
_PATH = re.compile(r"(?:~|[\w.@+-]+)?(?:/[\w.@+-]+){2,}")
# Lines that state an error; checked in order of usefulness per class.
_EXCEPTION = re.compile(r"^\s*((?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Warning))(?::.*)?$")
_FAILED_TEST = re.compile(r"^(FAIL|ERROR): (\w+) \(([\w.]+)\)")


def redact(text: str | None, limit: int = MAX_EXCERPT) -> str:
    """One printable line, privacy-bounded; empty input gives UNKNOWN."""
    if not isinstance(text, str):
        return UNKNOWN
    line = next((part for part in text.splitlines() if part.strip()), "")
    line = "".join(char if char.isprintable() else " " for char in line).strip()
    line = line.replace(_HOME, "~")
    line = _USERS.sub("~", line)
    line = _TMP.sub("<tmp>", line)
    line = _SECRET_PAIR.sub(lambda match: match.group(1) + match.group(2) + "[redacted]", line)
    line = _KNOWN_TOKEN.sub("[redacted]", line)
    line = _LONG_RUN.sub(lambda match: "[redacted]" if re.search(r"\d", match.group())
                         and re.search(r"[A-Za-z]", match.group()) else match.group(), line)
    line = _HEX.sub("[hex]", line)
    line = re.sub(r"\s+", " ", line).strip()
    if not line:
        return UNKNOWN
    return _elide(line, limit)


def _elide(line: str, limit: int) -> str:
    """Shorten from the front/middle so the tail (file name, final message) survives.

    Long paths lose their leading parts first ('…/kindle-project/telemetry/x.py');
    if the line is still too long, its middle is cut ('head … tail')."""
    while len(line) > limit:
        paths = [match for match in _PATH.finditer(line) if match.group().count("/") >= 2]
        if not paths:
            break
        match = max(paths, key=lambda item: len(item.group()))
        parts = match.group().split("/")
        keep = len(parts) - 1
        while keep > 1 and len(line) - len(match.group()) + len("…/" + "/".join(parts[-keep:])) > limit:
            keep -= 1
        short = "…/" + "/".join(parts[-keep:])
        if len(short) >= len(match.group()):
            break
        line = line[:match.start()] + short + line[match.end():]
    if len(line) > limit:
        # Cut the middle at token boundaries (space . / :) so both ends stay readable:
        # the tail starts at a boundary (ahead if near, else just before it, shrinking
        # the head), and the head ends at one.
        def boundary_before(end: int, span: int) -> int:
            return max(line.rfind(char, max(0, end - span), end) for char in " ./:")

        head = limit * 2 // 5
        start = len(line) - (limit - head - 3)
        ahead = [index for index in (line.find(char, start, start + 30) for char in " ./:") if index >= 0]
        if ahead:
            start = min(ahead) + 1
        else:
            back = boundary_before(start, 40)
            if back > 0 and len(line) - back - 1 <= limit - 23:
                start = back + 1
                head = limit - 3 - (len(line) - start)
        cut = boundary_before(head, 30)
        kept = line[:cut + 1].rstrip(" ./:") if cut > 0 else line[:head].rstrip()
        line = kept + " … " + line[start:].lstrip()
    return line


def first_error_line(output: str, error_class: str | None) -> str:
    """The single most telling error line from a failed run's output, redacted. Read from the
    end (audit 21 Oct): the last exception is the one that ended the run."""
    lines = output.splitlines() if isinstance(output, str) else []
    if error_class == "no_tests_discovered":
        found = next((line for line in lines if line.strip() == "NO TESTS RAN"), None) \
            or next((line for line in lines if re.match(r"^Ran 0 tests", line)), None)
        return redact(found)
    exceptions = [line for line in lines if _EXCEPTION.match(line)]
    if error_class == "import_error":
        exceptions = ([line for line in exceptions if "ModuleNotFoundError" in line]
                      or [line for line in exceptions if "ImportError" in line] or exceptions)
    return redact(exceptions[-1]) if exceptions else UNKNOWN


def _start_dir(command: str | None) -> str | None:
    try:
        tokens = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return None
    for flag, value in zip(tokens, tokens[1:]):
        if flag in ("-s", "--start-directory") and re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", value) \
                and not value.startswith((".", "/")):
            return value.rstrip("/")
    return None


def failing_test(output: str, command: str | None = None) -> str:
    """'test_name in tests/test_mod.py' from unittest's FAIL/ERROR header, else UNKNOWN.

    With `discover -s <dir>` unittest names modules relative to <dir>, so the
    command's start folder is prefixed to give the real file path. The last header wins,
    matching the error line, which is also read from the end."""
    start = _start_dir(command)
    for line in reversed(output.splitlines()) if isinstance(output, str) else []:
        match = _FAILED_TEST.match(line)
        if not match or match.group(3).startswith("unittest."):
            continue
        parts = match.group(3).split(".")
        module = []
        for part in parts:
            if part[:1].isupper() or part == match.group(2):
                break
            module.append(part)
        if module:
            path = "/".join(module) + ".py"
            if start and len(module) == 1:
                path = f"{start}/{path}"
            return redact(f"{match.group(2)} in {path}", MAX_FIX)
    return UNKNOWN


# Cursor's hook error types (bench_metrics_hook.py) in plain words; others are not shown.
CURSOR_ERROR_TYPES = {
    "environment": "a file, path or permission problem", "timeout": "a timeout",
    "invalid_arguments": "invalid arguments", "provider": "a model provider error (rate limit or outage)",
    "user_abort": "a cancel",
}


def step_label(workflow_id: str, step_id: str, tool: str = UNKNOWN) -> str:
    if workflow_id == "wf:samantha":
        return f"Samantha's {tool if tool != UNKNOWN else 'voice session'}"
    if workflow_id == "wf:agent-commands":
        return f"running {step_id.removeprefix('step:')}"
    if workflow_id == "wf:agent-tools":
        return f"using Cursor's {tool if tool != UNKNOWN else step_id.removeprefix('step:')} tool"
    return STEP_LABELS.get((workflow_id, step_id), "a tracked workflow step")


def headline(error_class: str, project: str, *, source: str = "codex", step_id: str = UNKNOWN,
             tool: str = UNKNOWN, test: str = UNKNOWN) -> str:
    where = f" in {project}" if project != UNKNOWN else ""
    if error_class in ("test_failures", "test_errors", "nonzero_exit") and test != UNKNOWN:
        name, _, file = test.partition(" in ")
        named = f"{name} fails{where}" if name.startswith("test") else f"{file or name} fails{where}"
        if len(named.split()) <= 10 and len(named) <= 90:
            return named
    if error_class == "samantha_failure":
        return f"Samantha's {tool if tool != UNKNOWN else 'voice session'} keeps failing"
    if error_class == "repeated_failure":
        if source == "cursor":
            return f"Cursor's {tool if tool != UNKNOWN else 'tool'} calls fail repeatedly"
        program = step_id.removeprefix("step:") if step_id != UNKNOWN else "A command"
        return f"{program} keeps failing{where}"
    return {
        "import_error": "Tests fail to import",
        "no_tests_discovered": "unittest finds no tests",
        "test_failures": "Tests fail",
        "test_errors": "Tests crash with errors",
        "nonzero_exit": "Test run exits with an error",
        "exit0_unverified": "Test run shows no pass result",
    }.get(error_class, "Test runs fail") + where


def what_happened(error_class: str, workflow_id: str, step_id: str, *, tool: str = UNKNOWN,
                  error_type: str = UNKNOWN, source: str = "codex") -> str:
    step = step_label(workflow_id, step_id, tool)
    if error_class == "samantha_failure":
        return (f"{step} failed in more than one voice session (Samantha's activity log; "
                "no transcript or reply text is read).")
    if error_class == "repeated_failure" and source == "grokbot":
        return (f"While {step}, commands starting with it failed with the same exit code again and again "
                "within minutes (Grok Bot's capture keeps only the first word, so it may be different commands).")
    if error_class == "repeated_failure":
        if workflow_id == "wf:agent-tools":
            kind = CURSOR_ERROR_TYPES.get(error_type)
            return f"While {step}, calls failed again and again in one agent turn" + (f", from {kind}." if kind else ".")
        return f"While {step}, the identical command failed again and again in one agent turn (a retry loop)."
    return {
        "import_error": f"While {step}, Python could not import a test module, so those tests never ran.",
        "no_tests_discovered": f"While {step}, unittest found 0 tests, so nothing was checked.",
        "test_failures": f"While {step}, at least one test failed its assertion.",
        "test_errors": f"While {step}, a test raised an unexpected error.",
        "nonzero_exit": f"While {step}, the test command exited with an error.",
        "exit0_unverified": f"While {step}, the test command exited 0 but printed no passing result.",
    }.get(error_class, f"While {step}, runs failed in more than one way.")


def fix(error_class: str, command: str, error_line: str, test: str) -> str:
    """Concrete next step from the evidence; UNKNOWN when no rule fits."""
    if error_class == "import_error":
        match = re.search(r"No module named '([\w.]+)'", error_line)
        if match:
            parts = match.group(1).split(".")
            if len(parts) >= 2 and parts[0] == "tests":
                return redact(f"Add tests/__init__.py, or run: python3 -m unittest discover -s tests "
                              f"-p '{parts[-1]}.py'")
            return redact(f"Install {parts[0]} or run from the repo root so it can be imported")
        return UNKNOWN
    if error_class == "no_tests_discovered":
        try:
            tokens = shlex.split(command) if command != UNKNOWN else []
        except ValueError:
            tokens = []
        if "discover" in tokens and "-s" not in tokens:
            return "Give the start folder: python3 -m unittest discover -s tests"
        if "-p" in tokens:
            return "Check that the -p pattern matches the test file names"
        return UNKNOWN
    if error_class == "test_failures":
        return _test_fix("Fix the failing test", test) if test != UNKNOWN \
            else "Fix the first failing assertion in the run output, then re-run"
    if error_class == "test_errors":
        return _test_fix("Fix the error raised by", test) if test != UNKNOWN else UNKNOWN
    if error_class == "exit0_unverified":
        return "Re-run the tests and check the output ends with OK"
    return UNKNOWN


def _test_fix(prefix: str, test: str) -> str:
    name, _, path = test.partition(" in ")
    return redact(f"{prefix} {name} ({path}), then re-run it", MAX_FIX)


def local_clock(value: str, reference: str) -> str:
    """'14:04' in the Mac's local zone, or '19 Oct 14:04' when not on the reference day."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    today = datetime.fromisoformat(reference.replace("Z", "+00:00")).astimezone().date()
    return moment.strftime("%H:%M") if moment.date() == today else moment.strftime("%-d %b %H:%M")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_stale(failed_runs: int, last_seen: str, generated_at: str) -> bool:
    """The staleness rule: nothing for 24 h, or a single failed run and nothing for 2 h."""
    quiet = (_instant(generated_at) - _instant(last_seen)).total_seconds()
    return quiet > STALE_ALWAYS_AFTER_S or (failed_runs <= STALE_MAX_FAILED_RUNS and quiet > STALE_AFTER_S)


def status_sentence(status: str, last_seen: str, fixed_at: str, generated_at: str,
                    error_class: str = UNKNOWN) -> str:
    """Only what the evidence shows: a confirmed passing run, or none since the last failure."""
    if status == "recovered" and fixed_at != UNKNOWN:
        if error_class == "repeated_failure":
            return f"Fixed: the same command succeeded at {local_clock(fixed_at, generated_at)}, no repeat since"
        return f"Fixed: tests passed at {local_clock(fixed_at, generated_at)}, no repeat since"
    if status == "stale":
        return f"Possibly stale · last failed {local_clock(last_seen, generated_at)}, not seen since"
    return f"Still failing: last failed at {local_clock(last_seen, generated_at)}, no confirmed fix since"


_ONE_FILE = re.compile(r"(?:^|\s)-p\s+['\"]?[^'\"\s*?\[]+\.py['\"]?(?:\s|$)")
_DOTTED = re.compile(r"-m\s+unittest\s+(?:-\S+\s+)*tests?\.\w")


_QUOTE_CHARS = re.compile(r"[\'\"\\]")


def unquoted(command: str) -> str:
    """A command with every quote and backslash removed and spaces collapsed: two commands
    that differ only in quoting compare equal."""
    return " ".join(_QUOTE_CHARS.sub("", command).split())


def pass_scope(command: str, failed_commands: set[str] | frozenset[str]) -> str:
    """How a passing check relates to the failures it recovered (20 Oct 19:55 review):
    "same" command, "one_file" (unittest -p <one file>), "narrower" (a dotted unittest
    target), "requoted" (the same command, quoted differently), "other" command, or
    "unknown". Only "same" can show the failing command works."""
    if command == UNKNOWN or not command:
        return "unknown"
    if command in failed_commands:
        return "same"
    # 21 Oct: the same command with only its quoting/escaping changed (C2, 20 Oct 14:08:
    # `-c 'print("…")'` failed through the VM runner, `-c "'print(\"…\")'"` passed).
    if unquoted(command) in {unquoted(failed) for failed in failed_commands}:
        return "requoted"
    if _ONE_FILE.search(command):
        return "one_file"
    if _DOTTED.search(command):
        return "narrower"
    return "other"
