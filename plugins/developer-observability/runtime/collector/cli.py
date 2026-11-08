"""`developer-observability collect`: scan local sources and write findings.json (stdlib only)."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
import time

RUNTIME = Path(__file__).resolve().parents[1]
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from collector.analyze import ACTIVE_WINDOW_H, HOTSPOT_MS, TEST_HOTSPOT_MS, build  # noqa: E402
from collector.sources import iter_spec, known_sources, ref  # noqa: E402
from collector import retention  # noqa: E402

DEFAULT_OUT = '~/.local/share/developer-observability/collected'
DEFAULT_CONFIG = '~/.config/developer-observability/config.json'


def load_config(path=None):
    path = Path(path or os.environ.get('OBSERVABILITY_CONFIG') or DEFAULT_CONFIG).expanduser()
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def collector_settings(config):
    settings = config.get('collector') if isinstance(config.get('collector'), dict) else {}
    return settings


def out_dir(config, override=None):
    return Path(override or collector_settings(config).get('out') or DEFAULT_OUT).expanduser()


def configured_specs(config, include_known=True):
    settings = collector_settings(config)
    specs = [s for s in settings.get('sources', []) if isinstance(s, dict)]
    if include_known and settings.get('known_sources', True) is not False:
        specs.extend(known_sources())
    return specs


def _peak_rss_mb():
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == 'darwin' else peak / 1024, 1)


def _write(out, document):
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.findings-', suffix='.json', dir=out)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(document, handle, separators=(',', ':'))
            if handle.tell() > 128_000_000:
                raise ValueError('collector_failed: snapshot too large; narrow collector sources')
        os.chmod(tmp, 0o600)
        os.replace(tmp, out / 'findings.json')
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out / 'findings.json'


def analysis_settings(config, include_tests=None):
    settings = collector_settings(config)
    def number(key, default):
        value = settings.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default
    return {'include_tests': bool(settings.get('include_tests', False)) if include_tests is None else bool(include_tests),
            'window_h': number('active_window_h', ACTIVE_WINDOW_H),
            'threshold_ms': number('latency_threshold_ms', HOTSPOT_MS),
            'test_threshold_ms': number('test_latency_threshold_ms', TEST_HOTSPOT_MS)}


def collect(specs, out, now=None, include_tests=False, window_h=ACTIVE_WINDOW_H, threshold_ms=HOTSPOT_MS,
            test_threshold_ms=TEST_HOTSPOT_MS, config=None, home=None):
    """Write every finding (ranked, never capped) and return counts that match what the read side lists.

    0.1.8: with a config, prune expired plugin-owned files afterwards and add `storage` findings."""
    started = time.perf_counter()
    stats = {}

    def events():
        for spec in specs:
            yield from iter_spec(spec, stats)

    collected = list(events())
    document = build(collected, stats, now, include_tests, window_h, threshold_ms, test_threshold_ms)
    pruned = storage = None
    if config is not None:
        pruned = retention.prune(config, out, now=now, home=home)
        storage = retention.report(config, out, home=home)
        document['storage'] = storage
        document['findings'].extend(retention.storage_findings(storage, document['generated_at']))
        for rank, finding in enumerate(document['findings'], 1):
            finding['rank'] = rank
    path = _write(Path(out).expanduser(), document)
    findings = document['findings']
    return {'out': ref(path, 0)[:-2], 'findings': len(findings), 'total': len(findings),
            'active': sum(f['status'] == 'active' for f in findings),
            'by_status': dict(Counter(f['status'] for f in findings)),
            'by_kind': dict(Counter(f['kind'] for f in findings)),
            'by_project': dict(Counter(f['latest_evidence']['project'] for f in findings)),
            'excluded_test_events': sum(item.get('excluded_test', 0) for item in stats.values()),
            'mirrored_events': sum(item.get('mirrors', 0) for item in stats.values()),
            'include_tests': include_tests,
            'events': len(collected), 'sources': len(stats),
            'runtime_ms': int((time.perf_counter() - started) * 1000), 'peak_rss_mb': _peak_rss_mb(),
            'generated_at': document['generated_at'],
            **({'pruned': {k: pruned[k] for k in ('moved', 'bytes', 'capped') if k in pruned},
                'storage_bytes': storage['total_bytes'],
                'storage_over_threshold': [r['path'] for r in storage['paths'] if r['over_threshold']]}
               if storage is not None else {})}


def storage(config, out, dry_run=True, home=None):
    """Storage report plus a prune pass (dry run by default) without collecting."""
    return {'prune': retention.prune(config, out, dry_run=dry_run, home=home),
            'storage': retention.report(config, out, home=home)}


def parse_source(text):
    parts = text.split(':')
    if len(parts) < 3 or not parts[0] or not parts[-1]:
        raise ValueError('use --source TYPE:PATH:PROJECT')
    return {'type': parts[0], 'path': ':'.join(parts[1:-1]), 'project': parts[-1]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='developer-observability collect', description=__doc__)
    parser.add_argument('--out')
    parser.add_argument('--source', action='append', default=[], help='TYPE:PATH:PROJECT; TYPE jsonl|textlog|junit|checklog|git|hygiene')
    parser.add_argument('--project-dir', action='append', default=[])
    parser.add_argument('--no-known', action='store_true', help='skip the known local sources')
    parser.add_argument('--config', help='read-only; never written')
    parser.add_argument('--include-tests', action='store_true', help='count test-fixture and self-test sessions as real use')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--no-prune', action='store_true', help='skip 0.1.8 retention and storage findings')
    parser.add_argument('--storage', action='store_true', help='report storage and a dry-run prune; no collect')
    parser.add_argument('--prune', action='store_true', help='with --storage: actually move expired files')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.storage:
        print(json.dumps(storage(config, out_dir(config, args.out), dry_run=not args.prune)))
        return 0
    try:
        specs = configured_specs(config, not args.no_known) + [parse_source(s) for s in args.source]
    except ValueError as error:
        print(f'developer-observability collect: {error}', file=sys.stderr)
        return 2
    specs += [{'type': 'project_dir', 'path': d} for d in args.project_dir]
    summary = collect(specs, out_dir(config, args.out), **analysis_settings(config, True if args.include_tests else None),
                      config=None if args.no_prune else config)
    if args.json:
        print(json.dumps(summary))
    else:
        print(f"collected {summary['findings']} findings from {summary['events']} events in "
              f"{summary['runtime_ms']} ms -> {summary['out']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
