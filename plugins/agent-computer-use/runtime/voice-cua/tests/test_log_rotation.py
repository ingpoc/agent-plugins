"""0.2.86: samantha-activity.jsonl and voice.log rotate in the writer. Temp directories only, never the real cache."""
import gzip
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
_ACTIVITY_DIR = tempfile.TemporaryDirectory(prefix="voice-cua-test-rotation-")
os.environ["VOICE_CUA_ACTIVITY_LOG"] = str(Path(_ACTIVITY_DIR.name) / "activity.jsonl")
from voice_cua import log_rotation  # noqa: E402


def _all_lines(live: Path) -> list[str]:
    lines: list[str] = []
    for rotated in reversed(log_rotation.rotated_files(live)):
        opener = gzip.open if rotated.suffix == ".gz" else open
        with opener(rotated, "rt", encoding="utf-8") as fh:
            lines.extend(fh.read().splitlines())
    if live.exists():
        lines.extend(live.read_text(encoding="utf-8").splitlines())
    return lines


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cua-rot-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.assertFalse(str(self.dir).startswith(str(Path.home() / ".cache")))

    def test_names_limit_and_defaults(self):
        self.assertEqual(log_rotation.MAX_BYTES, 50 * 1024 * 1024)
        self.assertEqual(log_rotation.KEEP, 5)
        self.assertEqual(log_rotation.rotated_path(Path("/x/samantha-activity.jsonl"), 1).name, "samantha-activity.1.jsonl.gz")
        self.assertEqual(log_rotation.rotated_path(Path("/x/voice.log"), 5).name, "voice.5.log.gz")

    def test_rolls_at_limit_keeps_five_gzipped_and_drops_older(self):
        live = self.dir / "samantha-activity.jsonl"
        row = (json.dumps({"kind": "x", "pad": "p" * 80}) + "\n").encode()
        for i in range(400):  # ~36 KB at a 4 KB limit: many more rotations than kept
            log_rotation.append(live, row.replace(b'"x"', f'"{i}"'.encode()), limit=4096)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual([n for n in names if n != live.name],
                         [f"samantha-activity.{i}.jsonl.gz" for i in range(1, 6)], "exactly five, all gzipped")
        self.assertLess(live.stat().st_size, 4096 + len(row))
        kinds = [json.loads(line)["kind"] for line in _all_lines(live)]
        self.assertEqual(kinds, [str(i) for i in range(400 - len(kinds), 400)], "newest rows kept in order, no gaps")
        for gz in self.dir.glob("*.gz"):
            with gzip.open(gz, "rt") as fh:
                for line in fh:
                    json.loads(line)

    def test_never_touches_sockets_locks_or_other_files(self):
        live = self.dir / "samantha-activity.jsonl"
        sock_path = self.dir / "cua-service.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(str(sock_path))
        others = [self.dir / n for n in ("cua-service.sock.lock", "cua-service.relaunch.lock", "visual-focus.lock",
                                         "island-events.jsonl", "samantha-activity.jsonl.lock")]
        for p in others:
            p.write_text("keep")
        before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in [sock_path, *others]}
        for _ in range(200):
            log_rotation.append(live, b"y" * 99 + b"\n", limit=2048)
        for p, sig in before.items():
            self.assertEqual((p.stat().st_ino, p.stat().st_mtime_ns), sig, p.name)

    def test_concurrent_process_and_thread_writers_lose_and_duplicate_nothing(self):
        live = self.dir / "samantha-activity.jsonl"
        script = textwrap.dedent(f"""
            import sys, threading
            sys.path.insert(0, {str(ROOT / "python")!r})
            from pathlib import Path
            from voice_cua import log_rotation
            live, who = Path(sys.argv[1]), sys.argv[2]
            def run(t):
                for i in range(150):
                    log_rotation.append(live, f'{{{{"w":"{{who}}-{{t}}","i":{{i}},"pad":"{{"q"*60}}"}}}}\\n'.encode(), limit=49152)
            ts = [threading.Thread(target=run, args=(t,)) for t in range(3)]
            [t.start() for t in ts]; [t.join() for t in ts]
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(live), f"p{n}"]) for n in range(4)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        rows = [json.loads(line) for line in _all_lines(live)]   # every line is whole JSON
        seen = {(r["w"], r["i"]) for r in rows}
        self.assertEqual(len(seen), len(rows), "no duplicates")
        self.assertEqual(len(rows), 4 * 3 * 150, "nothing lost (total stays under 6 × limit)")
        self.assertGreaterEqual(len(list(self.dir.glob("*.gz"))), 2, "it really rotated under contention")
        self.assertLessEqual(len(list(self.dir.glob("*.gz"))), 5)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_activity_log_writer_rotates(self):
        from voice_cua import activity_log
        live = self.dir / "samantha-activity.jsonl"
        os.environ["VOICE_CUA_LOG_MAX_BYTES"] = "3000"
        self.addCleanup(os.environ.pop, "VOICE_CUA_LOG_MAX_BYTES", None)
        old = os.environ["VOICE_CUA_ACTIVITY_LOG"]
        os.environ["VOICE_CUA_ACTIVITY_LOG"] = str(live)
        self.addCleanup(os.environ.__setitem__, "VOICE_CUA_ACTIVITY_LOG", old)
        for i in range(100):
            activity_log.log_event("tool_call", tool="state", n=i, pad="z" * 40)
        self.assertTrue((self.dir / "samantha-activity.1.jsonl.gz").is_file())
        self.assertEqual([json.loads(line)["n"] for line in _all_lines(live)][-1], 99)

    def test_voice_log_stdout_rotates_inside_the_voice_process(self):
        live = self.dir / "voice.log"
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT / "python")!r})
            from pathlib import Path
            from voice_cua.log_rotation import install_voice_log_rotation
            assert install_voice_log_rotation(Path(sys.argv[1]), limit=4096)
            for i in range(300):
                print(f"[startup] phase=step{{i}} status=ok " + "v" * 40)
            print("last line", file=sys.stderr)
        """)
        with open(live, "w") as out:   # like CUAService: the child's stdout/stderr ARE voice.log
            subprocess.run([sys.executable, "-c", script, str(live)], stdout=out, stderr=out, check=True, timeout=60)
        self.assertTrue((self.dir / "voice.1.log.gz").is_file())
        lines = _all_lines(live)
        self.assertEqual(lines[-1].split(" ", 1)[1], "last line")
        phases = [line.split(" ", 1)[1].split()[1] for line in lines if "[startup]" in line]
        self.assertEqual(phases[-1], "phase=step299")
        nums = [int(p.removeprefix("phase=step")) for p in phases]
        self.assertEqual(nums, list(range(nums[0], 300)), "contiguous: nothing landed in a rotated copy after rotation")

    def test_voice_log_timestamps_each_complete_line_with_offset(self):
        stream = io.StringIO()
        wrapped = log_rotation.RotatingStdStreams(self.dir / "voice.log", stream)
        wrapped.write("split ")
        wrapped.write("line\nsecond\n")
        wrapped.flush()
        lines = stream.getvalue().splitlines()
        self.assertEqual([line.split(" ", 1)[1] for line in lines], ["split line", "second"])
        from datetime import datetime

        for line in lines:
            stamp = datetime.fromisoformat(line.split(" ", 1)[0])
            self.assertIsNotNone(stamp.utcoffset())


if __name__ == "__main__":
    unittest.main()
