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

from collector.analyze import build  # noqa: E402
from collector.sources import iter_spec, known_sources, ref  # noqa: E402

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
        os.chmod(tmp, 0o600)
        os.replace(tmp, out / 'findings.json')
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out / 'findings.json'


def collect(specs, out, now=None):
    started = time.perf_counter()
    stats = {}

    def events():
        for spec in specs:
            yield from iter_spec(spec, stats)

    collected = list(events())
    document = build(collected, stats, now)
    path = _write(Path(out).expanduser(), document)
    findings = document['findings']
    return {'out': ref(path, 0)[:-2], 'findings': len(findings),
            'by_kind': dict(Counter(f['kind'] for f in findings)),
            'by_project': dict(Counter(f['latest_evidence']['project'] for f in findings)),
            'events': len(collected), 'sources': len(stats),
            'runtime_ms': int((time.perf_counter() - started) * 1000), 'peak_rss_mb': _peak_rss_mb(),
            'generated_at': document['generated_at']}


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
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    try:
        specs = configured_specs(config, not args.no_known) + [parse_source(s) for s in args.source]
    except ValueError as error:
        print(f'developer-observability collect: {error}', file=sys.stderr)
        return 2
    specs += [{'type': 'project_dir', 'path': d} for d in args.project_dir]
    summary = collect(specs, out_dir(config, args.out))
    if args.json:
        print(json.dumps(summary))
    else:
        print(f"collected {summary['findings']} findings from {summary['events']} events in "
              f"{summary['runtime_ms']} ms -> {summary['out']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
