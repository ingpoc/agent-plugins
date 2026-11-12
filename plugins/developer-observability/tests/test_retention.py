"""0.1.8 retention: dry run, age cutoff, backups kept longer, trash cap, ACU/Samantha exclusions, storage findings.

Every path lives in a temp dir under this worktree's tests/ folder; nothing under the real home is read or written."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))

from collector import retention  # noqa: E402
from collector.cli import collect  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400


def touch(path, age_days, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)
    os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
    return path


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix='.retention-')
        self.home = Path(self._tmp.name)
        self.out = self.home / '.local/share/developer-observability/collected'
        self.cache = self.home / '.cache/observability'
        self.traces = self.home / 'traces'
        self.trash = self.home / '.Trash/developer-observability-pruned'
        self.config = {'retention': {'cache_dirs': [str(self.cache)], 'trace_paths': [str(self.traces)],
                                     'trash_dir': str(self.trash), 'warn_bytes': 1000}}

    def tearDown(self):
        self._tmp.cleanup()

    def acu(self):
        acu = self.home / '.cache/macos-cua'
        return [touch(acu / 'samantha-activity.jsonl', 90, 2000), touch(acu / 'samantha-activity.1.jsonl', 90),
                touch(acu / 'voice.log', 90), touch(acu / 'cua.sock.lock', 90)]

    def test_dry_run_moves_nothing(self):
        old = touch(self.cache / 'check.log', 10)
        result = retention.prune(self.config, self.out, dry_run=True, now=NOW, home=self.home)
        self.assertEqual(result['moved'], 1)
        self.assertTrue(old.exists())
        self.assertFalse(self.trash.exists())

    def test_age_cutoff_and_backups(self):
        old = touch(self.cache / 'check.log', 8)
        fresh = touch(self.cache / 'handoff.md', 6)
        bak = touch(self.cache / 'config.json.bak-20261001', 20)
        old_bak = touch(self.cache / 'mcp.json.bak-20260901', 31)
        live = touch(self.out / 'findings.json', 100)
        scratch = touch(self.out / '.findings-abc.json', 8)
        trace = touch(self.traces / 'spans.jsonl', 8)
        result = retention.prune(self.config, self.out, now=NOW, home=self.home)
        self.assertEqual(result['moved'], 4)
        for kept in (fresh, bak, live):
            self.assertTrue(kept.exists(), kept.name)
        for gone in (old, old_bak, scratch, trace):
            self.assertFalse(gone.exists(), gone.name)
        moved = sorted(p.name for p in self.trash.rglob('*') if p.is_file())
        self.assertEqual(moved, sorted(['check.log', 'mcp.json.bak-20260901', '.findings-abc.json', 'spans.jsonl']))

    def test_expired_superseded_snapshot_directory_moves_to_trash(self):
        snapshot_id = 'a' * 32
        staged = self.out / '.trash' / snapshot_id
        touch(staged / 'sessions' / 'session.json', 10)
        old = NOW - 10 * DAY
        os.utime(staged, (old, old))
        (self.out / 'findings.json').parent.mkdir(parents=True, exist_ok=True)
        (self.out / 'findings.json').write_text(json.dumps({'snapshot_id': 'b' * 32}))

        result = retention.prune(self.config, self.out, now=NOW, home=self.home)

        self.assertEqual(result['moved'], 1)
        self.assertFalse(staged.exists())
        moved = list(self.trash.rglob('session.json'))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_bytes(), b'x' * 10)

    def test_candidates_skip_kept_snapshot_and_fresh_trash(self):
        snapshot_id = 'b' * 32
        snap = self.out / 'snapshots' / snapshot_id
        for i in range(20):
            touch(snap / 'sessions' / f's{i}.json', 10)
        (self.out / 'findings.json').parent.mkdir(parents=True, exist_ok=True)
        (self.out / 'findings.json').write_text(json.dumps({'snapshot_id': snapshot_id}))
        staged = self.out / '.trash' / ('c' * 32)
        for i in range(20):
            touch(staged / 'sessions' / f's{i}.json', 10)
        os.utime(staged, (NOW - DAY, NOW - DAY))
        scratch = touch(self.out / '.findings-old.json', 8)
        keep = {self.out / 'findings.json', snap}
        found = [(path, age) for path, root, age in retention._candidates(
            self.out, keep, NOW, 7, 30, self.home)]
        self.assertEqual(found, [(scratch, 8)])
        started = time.perf_counter()
        result = retention.prune(self.config, self.out, now=NOW, home=self.home)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(result['moved'], 1)
        self.assertTrue(snap.exists())
        self.assertTrue(staged.exists())
        self.assertFalse(scratch.exists())

    def test_never_touches_acu_or_samantha_logs(self):
        protected = self.acu()
        before = {p: (p.stat().st_size, p.stat().st_mtime) for p in protected}
        # Even when misconfigured to point at ACU's dir, nothing there moves.
        self.config['retention']['cache_dirs'].append(str(self.home / '.cache/macos-cua'))
        self.config['retention']['trace_paths'].append(str(self.home / '.cache'))
        touch(self.home / '.cache/other/voice.log', 90)
        result = retention.prune(self.config, self.out, now=NOW, home=self.home)
        self.assertTrue(all(item['name'] != 'voice.log' for item in result['items']))
        self.assertEqual(before, {p: (p.stat().st_size, p.stat().st_mtime) for p in protected})
        self.assertTrue((self.home / '.cache/other/voice.log').exists())
        self.assertTrue(retention.is_protected(self.home / '.cache/macos-cua/anything', self.home))

    def test_machine_toml_protected_paths(self):
        kept = touch(self.cache / 'keep/old.log', 30)
        gone = touch(self.cache / 'old.log', 30)
        (self.home / '.agents').mkdir()
        (self.home / '.agents/machine.toml').write_text('[protected]\npaths = ["~/.cache/observability/keep"]\n')
        retention.prune(self.config, self.out, now=NOW, home=self.home)
        self.assertTrue(kept.exists())
        self.assertFalse(gone.exists())

    def test_machine_toml_missing_or_without_list(self):
        self.assertEqual(retention.machine_protected(self.home), [])
        (self.home / '.agents').mkdir()
        (self.home / '.agents/machine.toml').write_text('[roots]\nx = "y"\n')
        self.assertEqual(retention.machine_protected(self.home), [])
        (self.home / '.agents/machine.toml').write_text('not toml [[[')
        self.assertEqual(retention.machine_protected(self.home), [])
        touch(self.cache / 'old.log', 30)
        self.assertEqual(retention.prune(self.config, self.out, now=NOW, home=self.home)['moved'], 1)

    def test_trash_cap(self):
        self.config['retention']['max_trash_bytes'] = 25
        for i in range(4):
            touch(self.cache / f'f{i}.log', 9, 4096)
        result = retention.prune(self.config, self.out, now=NOW, home=self.home)
        self.assertTrue(result['capped'])
        self.assertEqual(result['moved'], 0)

    def test_report_and_storage_findings(self):
        self.acu()
        machine = self.home / 'Library/Application Support/com.apple.container/plugin-state/machine-apiserver/machines/kindle-signoz'
        touch(machine / 'rootfs.ext4', 0, 5000)
        storage = retention.report(self.config, self.out, home=self.home)
        labels = {row['path']: row for row in storage['paths']}
        self.assertIn('samantha_log:samantha-activity.jsonl', labels)
        self.assertIn('container_vm:kindle-signoz', labels)
        findings = retention.storage_findings(storage, '2026-11-04T00:00:00Z')
        kinds = {f['storage']['path']: f['latest_evidence']['project'] for f in findings}
        self.assertEqual(kinds['container_vm:kindle-signoz'], 'developer-observability')
        self.assertEqual(kinds['samantha_log:samantha-activity.jsonl'], 'samantha')
        self.assertTrue(all(f['kind'] == 'storage' for f in findings))

    def test_collect_prunes_and_adds_storage(self):
        touch(self.cache / 'old.log', 30, 5000)
        summary = collect([], self.out, now=NOW, config=self.config, home=self.home)
        self.assertEqual(summary['pruned']['moved'], 1)
        document = json.loads((self.out / 'findings.json').read_text())
        self.assertIn('storage', document)
        self.assertTrue((self.out / 'findings.json').exists())

    def test_disabled(self):
        old = touch(self.cache / 'old.log', 30)
        self.config['retention']['enabled'] = False
        self.assertEqual(retention.prune(self.config, self.out, now=NOW, home=self.home)['moved'], 0)
        self.assertTrue(old.exists())


if __name__ == '__main__':
    unittest.main()
