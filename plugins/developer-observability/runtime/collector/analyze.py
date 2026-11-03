"""Turn normalized events into findings: root-cause failure clusters, latency hotspots and redundancy.

Accuracy rules (0.1.5):
- one occurrence per (session, cause) however many sources or kinds logged it (mirrors and double logging);
- mirror kinds (island HUD) and test sessions never count as failures (tests: unless include_tests);
- latency only from real step durations, against a threshold, with p50/p95;
- status from a recent window and, when sessions carry a version, from the latest version;
- nothing is dropped: findings are ranked, and the read side paginates.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import math
import time

from . import VERSION
from .scrub import scrub, signature
from .sources import iso

HOTSPOT_MS = 1000
TEST_HOTSPOT_MS = 60_000
MIN_SAMPLES = 5
ACTIVE_WINDOW_H = 24
BURST_GAP_S = 2
DUPLICATE_WINDOW_S = 10
RETRY_WORDS = ('retry', 'reconnect', 'resend')


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _key(parts):
    return 'col-' + hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:16]


def _seconds(ms):
    return f'{ms / 1000:.1f} s' if ms >= 1000 else f'{ms:.0f} ms'


def _label(event):
    return f"{event['kind']}/{event['step']}" if event['step'] else event['kind']


class Context:
    """Per-run facts shared by the analyses: version order per project, window and threshold."""

    def __init__(self, events, now, window_h, threshold_ms, test_threshold_ms=TEST_HOTSPOT_MS):
        self.now, self.window_s = now, window_h * 3600
        self.threshold_ms, self.test_threshold_ms = threshold_ms, test_threshold_ms
        first = defaultdict(dict)
        for e in events:
            if e['version'] and e['ts'] is not None:
                seen = first[e['project']].get(e['version'])
                if seen is None or e['ts'] < seen:
                    first[e['project']][e['version']] = e['ts']
        self.latest = {project: max(order, key=order.get) for project, order in first.items()}

    def recent(self, ts):
        return ts is not None and self.now - ts <= self.window_s


def prepare(events, include_tests=False):
    """Propagate session version/test flags across sources; split out excluded test events."""
    events = list(events)
    tests = {e['session'] for e in events if e['test'] and e['session']}
    versions = {}
    for e in events:
        if e['version'] and e['session']:
            versions.setdefault(e['session'], e['version'])
    kept, excluded = [], Counter()
    for e in events:
        if e['session'] in tests:
            e['test'] = True
        if not e['version'] and e['session'] in versions:
            e['version'] = versions[e['session']]
        if e['test'] and not include_tests:
            excluded[e['source']] += 1
            continue
        kept.append(e)
    return kept, excluded


def _status(ctx, project, last_ts, by_version):
    """Failures: none on the project's latest version -> recovered. Otherwise a recent window decides."""
    latest = ctx.latest.get(project)
    if by_version is not None and latest is not None and not by_version.get(latest):
        return 'recovered'
    return 'active' if ctx.recent(last_ts) else 'stale'


def _finding(ctx, kind, error_class, parts, events, headline, count, extra=None, by_version=None):
    project = events[0]['project']
    stamped = sorted((e for e in events if e['ts'] is not None), key=lambda e: e['ts'])
    first = stamped[0]['ts'] if stamped else ctx.now
    last = stamped[-1]['ts'] if stamped else ctx.now
    newest = stamped or events
    lead = Counter((e['kind'], e['step']) for e in events if e['step']).most_common(1)
    step = lead[0][0][1] if lead else (events[0]['step'] or events[0]['kind'])
    message = next((e['message'] for e in reversed(newest) if e['message']), '')
    times = [iso(e['ts']) for e in stamped[-64:]]
    sources = Counter(e['source'] for e in events)
    finding = {
        'finding_key': _key([*parts, kind]), 'kind': kind, 'error_class': error_class,
        'headline': scrub(headline, 160), 'source': sources.most_common(1)[0][0], 'owner_ref': f'owner:{project}',
        'status': _status(ctx, project, last, by_version),
        'first_seen': iso(first), 'last_seen': iso(last),
        'failed_attempt_count': count, 'confirmed_recovery_count': 0,
        'evidence_event_ids': [e['ref'] for e in newest[-20:]][::-1],
        'failure_attempt_times': times, 'failure_attempt_times_complete': count <= 64 and len(times) == count,
        'recovery_times': [], 'recovery_times_complete': True,
        'workflow_id': f"wf:{sources.most_common(1)[0][0]}", 'step_id': f'step:{project}-{step}',
        'latest_evidence': {'project': project, 'tool': step, 'first_error_line': message, 'error_type': events[0]['kind']},
    }
    if by_version and set(by_version) != {'unknown'}:
        finding['by_version'] = dict(by_version)
        latest = ctx.latest.get(project)
        if latest is not None:
            after = by_version.get(latest, 0)
            finding['before_after'] = {'version': latest, 'before': count - after, 'after': after}
    finding.update(extra or {})
    return finding


def _occurrences(items):
    """Within a source, a session counts the largest number any one kind/step logged for this cause
    (startup step + realtime_error for the same failure = 1). Across sources, the largest source wins
    (voice.log repeating the activity log adds nothing)."""
    best = (0, Counter())
    by_source = defaultdict(list)
    for e in items:
        by_source[e['source']].append(e)
    for rows in by_source.values():
        per_session, version_of = defaultdict(Counter), {}
        for e in rows:
            per_session[e['session']][(e['kind'], e['step'])] += 1
            version_of.setdefault(e['session'], e['version'])
        by_version = Counter()
        for session, counts in per_session.items():
            by_version[version_of.get(session) or 'unknown'] += max(counts.values())
        total = sum(by_version.values())
        if total > best[0]:
            best = (total, by_version)
    return best


def failures(ctx, events):
    """Root-cause clusters: (project, cause signature). Each session counts its occurrences once,
    taking the largest count any single source/kind logged for it, so mirrors are not double counted."""
    clusters = defaultdict(list)
    for e in events:
        if e['error'] and not e['mirror']:
            # A tool's failure stays with that tool; other kinds (startup step, realtime_error, log line) share a cause.
            tool = e['step'] if 'tool' in e['kind'] else ''
            clusters[(e['project'], signature(e['message']) or f"{e['kind']}/{e['step']}", tool)].append(e)
    out = []
    for parts, items in clusters.items():
        occurrences, by_version = _occurrences(items)
        logged = Counter()
        for e in items:
            logged[f"{e['source'].split(':')[-1]} {_label(e)}"] += 1
        lead = Counter(_label(e) for e in items if e['step']).most_common(1)
        label = lead[0][0] if lead else _label(items[0])
        condition = next((e['condition'] for e in items if e['condition']), None)
        cause = f' ({condition})' if condition else ''
        error_class = 'environment_condition' if condition else 'collector_failure'
        by_version = dict(by_version)
        out.append(_finding(ctx, 'failure', error_class, parts, items,
                            f"{parts[0]}: {label}{cause} failed {occurrences}x", occurrences,
                            {'logged_as': dict(logged.most_common(6)), **({'condition': condition} if condition else {})},
                            by_version))
    return out


def latency(ctx, events):
    """Hotspots from real durations only. When two sources log the same step (activity log and voice.log),
    the source with the most samples is used, so one measurement is never counted twice."""
    by_source = defaultdict(lambda: defaultdict(list))
    for e in events:
        if e['duration_ms'] is None or e['duration_ms'] < 0:
            continue
        by_source[(e['project'], e['step'] or e['kind'], e['metric'] or 'ms')][e['source']].append(e)
    out = []
    for parts, sources in by_source.items():
        items = max(sources.values(), key=len)
        values = [e['duration_ms'] for e in items]
        if len(values) < MIN_SAMPLES:
            continue
        p95 = percentile(values, 0.95)
        threshold = ctx.test_threshold_ms if parts[2] == 'test_ms' else ctx.threshold_ms
        if p95 < threshold:
            continue
        p50 = percentile(values, 0.5)
        stats = {'p50_ms': round(p50, 1), 'p95_ms': round(p95, 1), 'max_ms': round(max(values), 1),
                 'n': len(values), 'total_ms': round(sum(values), 1), 'threshold_ms': threshold,
                 'metric': parts[2]}
        metric = {'test_ms': 'duration', 'ms': 'duration'}.get(parts[2], parts[2][:-3] if parts[2].endswith('_ms') else parts[2])
        out.append(_finding(ctx, 'latency_hotspot', 'latency_hotspot', parts, items,
                            f"{parts[0]}: {parts[1]} {metric} slow p95 {_seconds(p95)} (p50 {_seconds(p50)}, "
                            f"n={len(values)}, threshold {_seconds(threshold)})",
                            len(values), {'latency': stats}))
    return out


def _redundancy(ctx, parts, items, pattern, occurrences, bursts, words):
    e = items[0]
    wasted = round(sum(x['duration_ms'] for x in items if x['duration_ms'] is not None), 1)
    return _finding(ctx, 'redundancy', 'redundancy', parts, items,
                    f"{e['project']}: {_label(e)} {words} {occurrences}x ({pattern})", occurrences,
                    {'redundancy': {'occurrences': occurrences, 'bursts': bursts, 'wasted_ms': wasted, 'pattern': pattern}})


def _close_run(run, bursts):
    if len(run) >= 3:
        e = run[0]
        entry = bursts[(e['project'], e['source'], e['kind'], e['step'])]
        entry[0] += len(run) - 1
        entry[1] += 1
        entry[2].extend(run[1:])


def redundancy(ctx, events):
    """Bursts of identical failures, retries/reconnects and duplicate calls. Metric rows never form bursts."""
    out = []
    streams = defaultdict(list)
    for e in events:
        if e['error'] and not e['mirror']:
            streams[(e['project'], e['source'])].append(e)
    bursts = defaultdict(lambda: [0, 0, []])
    for stream in streams.values():
        run = [stream[0]] if stream else []
        for prev, cur in zip(stream, stream[1:]):
            same = (cur['kind'], cur['step'], signature(cur['message'])) == (prev['kind'], prev['step'], signature(prev['message']))
            close = cur['ts'] is not None and prev['ts'] is not None and 0 <= cur['ts'] - prev['ts'] <= BURST_GAP_S
            if same and close:
                run.append(cur)
                continue
            _close_run(run, bursts)
            run = [cur]
        _close_run(run, bursts)
    for parts, (occurrences, count, items) in bursts.items():
        out.append(_redundancy(ctx, parts + ('burst',), items, 'burst', occurrences, count, 'repeated'))
    retries = defaultdict(list)
    for e in events:
        if not e['mirror'] and any(word in f"{e['kind']} {e['step']}".lower() for word in RETRY_WORDS):
            retries[(e['project'], e['source'], e['kind'], e['step'])].append(e)
    for parts, items in retries.items():
        if len(items) >= 3:
            out.append(_redundancy(ctx, parts + ('retry',), items, 'retry', len(items), 0, 'repeated'))
    last_call, duplicates = {}, defaultdict(list)
    for e in events:
        # One canonical call record per tool use; companion rows (comet_call) would double every call.
        if e['kind'] == 'tool_start' and e['ts'] is not None:
            key = (e['project'], e['source'], e['session'], e['step'], signature(e['message']))
            if key in last_call and e['ts'] - last_call[key] <= DUPLICATE_WINDOW_S:
                duplicates[(e['project'], e['source'], e['step'])].append(e)
            last_call[key] = e['ts']
    for parts, items in duplicates.items():
        if len(items) >= 2:
            out.append(_redundancy(ctx, parts + ('duplicate_call',), items, 'duplicate_call', len(items), 0, 'duplicated'))
    return out


def _rank(finding):
    active = finding['status'] == 'active'
    impact = finding.get('latency', {}).get('p95_ms', 0) / 1000 if finding['kind'] == 'latency_hotspot' \
        else finding['failed_attempt_count']
    return (active, impact)


def build(events, stats, now=None, include_tests=False, window_h=ACTIVE_WINDOW_H, threshold_ms=HOTSPOT_MS,
          test_threshold_ms=TEST_HOTSPOT_MS):
    now = time.time() if now is None else now
    events, excluded = prepare(events, include_tests)
    ctx = Context(events, now, window_h, threshold_ms, test_threshold_ms)
    findings = failures(ctx, events) + latency(ctx, events) + redundancy(ctx, events)
    findings.sort(key=_rank, reverse=True)
    for rank, finding in enumerate(findings, 1):
        finding['rank'] = rank
    for source, count in excluded.items():
        stats.setdefault(source, {})['excluded_test'] = count
    stamp = iso(now)
    return {
        'schema_version': 1, 'generated_at': stamp, 'producer': 'developer-observability-collector',
        'collector_version': VERSION, 'findings': findings,
        'settings': {'include_tests': include_tests, 'active_window_h': window_h, 'latency_threshold_ms': threshold_ms,
                     'test_latency_threshold_ms': test_threshold_ms},
        'latest_versions': dict(ctx.latest),
        'coverage': {source: 'collected' for source in stats},
        'coverage_details': {source: {'status': 'collected', 'collector_heartbeat_at': stamp,
                                      'last_event_at': item.get('last_event_at') or 'unknown',
                                      'last_received_at': stamp, 'dropped_rows': item.get('skipped', 0)}
                             for source, item in stats.items()},
        'coverage_gaps': [], 'coverage_gap_scope': 'collector', 'collector_stats': stats,
    }
