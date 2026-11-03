"""Turn normalized events into findings: failures, latency hotspots and redundancy."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import math
import time

from . import VERSION
from .scrub import scrub, signature
from .sources import iso

HOTSPOT_MS = 1000
MIN_SAMPLES = 5
HOTSPOTS_PER_PROJECT = 10
PER_PROJECT = 100
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


def _finding(kind, error_class, parts, events, headline, now, count, extra=None):
    project, source = events[0]['project'], events[0]['source']
    stamped = sorted((e for e in events if e['ts'] is not None), key=lambda e: e['ts'])
    first = stamped[0]['ts'] if stamped else now
    last = stamped[-1]['ts'] if stamped else now
    newest = sorted(events, key=lambda e: e['ts'] or 0)
    step = events[0]['step'] or events[0]['kind']
    message = next((e['message'] for e in reversed(newest) if e['message']), '')
    times = [iso(e['ts']) for e in stamped[-64:]]
    finding = {
        'finding_key': _key([*parts, kind]), 'kind': kind, 'error_class': error_class,
        'headline': scrub(headline, 160), 'source': source, 'owner_ref': f'owner:{project}',
        'status': 'active' if now - last <= 86400 else 'stale',
        'first_seen': iso(first), 'last_seen': iso(last),
        'failed_attempt_count': count, 'confirmed_recovery_count': 0,
        'evidence_event_ids': [e['ref'] for e in newest[-20:]][::-1],
        'failure_attempt_times': times, 'failure_attempt_times_complete': count <= 64 and len(times) == count,
        'recovery_times': [], 'recovery_times_complete': True,
        'workflow_id': f'wf:{source}', 'step_id': f'step:{project}-{step}',
        'latest_evidence': {'project': project, 'tool': step, 'first_error_line': message, 'error_type': events[0]['kind']},
    }
    finding.update(extra or {})
    return finding


def _label(event):
    return f"{event['kind']}/{event['step']}" if event['step'] else event['kind']


def failures(events, now):
    groups = defaultdict(list)
    for e in events:
        if e['error']:
            groups[(e['project'], e['source'], e['kind'], e['step'], signature(e['message']))].append(e)
    out = []
    for parts, items in groups.items():
        e = items[0]
        out.append(_finding('failure', 'collector_failure', parts, items,
                            f"{e['project']}: {_label(e)} failed {len(items)}x", now, len(items)))
    return out


def latency(events, now):
    groups = defaultdict(list)
    for e in events:
        if e['duration_ms'] is not None and e['duration_ms'] >= 0:
            groups[(e['project'], e['source'], e['kind'], e['step'])].append(e)
    per_project = defaultdict(list)
    for parts, items in groups.items():
        values = [e['duration_ms'] for e in items]
        if len(values) < MIN_SAMPLES:
            continue
        p95 = percentile(values, 0.95)
        if p95 < HOTSPOT_MS:
            continue
        p50 = percentile(values, 0.5)
        stats = {'p50_ms': round(p50, 1), 'p95_ms': round(p95, 1), 'max_ms': round(max(values), 1),
                 'n': len(values), 'total_ms': round(sum(values), 1)}
        e = items[0]
        per_project[e['project']].append(_finding(
            'latency_hotspot', 'latency_hotspot', parts, items,
            f"{e['project']}: {_label(e)} slow p95 {_seconds(p95)} (p50 {_seconds(p50)}, n={len(values)})",
            now, len(values), {'latency': stats}))
    out = []
    for items in per_project.values():
        out.extend(sorted(items, key=lambda f: f['latency']['p95_ms'], reverse=True)[:HOTSPOTS_PER_PROJECT])
    return out


def _redundancy(parts, items, pattern, occurrences, bursts, now, words):
    e = items[0]
    wasted = round(sum(x['duration_ms'] for x in items if x['duration_ms'] is not None), 1)
    return _finding('redundancy', 'redundancy', parts, items,
                    f"{e['project']}: {_label(e)} {words} {occurrences}x ({pattern})", now, occurrences,
                    {'redundancy': {'occurrences': occurrences, 'bursts': bursts, 'wasted_ms': wasted, 'pattern': pattern}})


def redundancy(events, now):
    out = []
    streams = defaultdict(list)
    for e in events:
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
        out.append(_redundancy(parts + ('burst',), items, 'burst', occurrences, count, now, 'repeated'))
    retries = defaultdict(list)
    for e in events:
        if any(word in f"{e['kind']} {e['step']}".lower() for word in RETRY_WORDS):
            retries[(e['project'], e['source'], e['kind'], e['step'])].append(e)
    for parts, items in retries.items():
        if len(items) >= 3:
            out.append(_redundancy(parts + ('retry',), items, 'retry', len(items), 0, now, 'repeated'))
    last_call, duplicates = {}, defaultdict(list)
    for e in events:
        if (e['kind'] == 'tool_start' or 'call' in e['kind']) and e['ts'] is not None:
            key = (e['project'], e['source'], e['session'], e['step'], signature(e['message']))
            if key in last_call and e['ts'] - last_call[key] <= DUPLICATE_WINDOW_S:
                duplicates[(e['project'], e['source'], e['step'])].append(e)
            last_call[key] = e['ts']
    for parts, items in duplicates.items():
        if len(items) >= 2:
            out.append(_redundancy(parts + ('duplicate_call',), items, 'duplicate_call', len(items), 0, now, 'duplicated'))
    return out


def _close_run(run, bursts):
    if len(run) >= 3:
        e = run[0]
        entry = bursts[(e['project'], e['source'], e['kind'], e['step'])]
        entry[0] += len(run) - 1
        entry[1] += 1
        entry[2].extend(run[1:])


def _rank(finding):
    latency = finding.get('latency', {}).get('p95_ms', 0)
    return (finding['failed_attempt_count'] if 'latency' not in finding else 0, latency)


def build(events, stats, now=None):
    now = time.time() if now is None else now
    events = list(events)
    findings = failures(events, now) + latency(events, now) + redundancy(events, now)
    per_project = defaultdict(list)
    for finding in findings:
        per_project[finding['latest_evidence']['project']].append(finding)
    kept = []
    for items in per_project.values():
        hot = [f for f in items if f['kind'] == 'latency_hotspot']
        rest = sorted((f for f in items if f['kind'] != 'latency_hotspot'), key=_rank, reverse=True)
        kept.extend(hot + rest[:max(0, PER_PROJECT - len(hot))])
    stamp = iso(now)
    return {
        'schema_version': 1, 'generated_at': stamp, 'producer': 'developer-observability-collector',
        'collector_version': VERSION, 'findings': kept,
        'coverage': {source: 'collected' for source in stats},
        'coverage_details': {source: {'status': 'collected', 'collector_heartbeat_at': stamp,
                                      'last_event_at': item.get('last_event_at') or 'unknown',
                                      'last_received_at': stamp, 'dropped_rows': item.get('skipped', 0)}
                             for source, item in stats.items()},
        'coverage_gaps': [], 'coverage_gap_scope': 'collector', 'collector_stats': stats,
    }
