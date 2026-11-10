"""Turn normalized events into findings: root-cause failure clusters, latency hotspots and redundancy.

Accuracy rules (0.1.5, 0.1.6, 0.1.7):
- one occurrence per (session, cause) however many sources or kinds logged it (mirrors and double logging);
- mirror kinds (island HUD) and test sessions never count as failures (tests: unless include_tests);
- latency only from real step durations, against a threshold, with p50/p95;
- status from a recent window and, when sessions carry a version, from the latest version;
- nothing is dropped: findings are ranked, and the read side paginates;
- 0.1.7: errors of one subsystem, session and turn within 30 s roll up under one root cause as `effects`
  and the incident's own tool calls are not reported again as redundancy;
- 0.1.7: duplicate calls need the same tool and normalized arguments, else the repeat is a retry;
- 0.1.7: tool and app errors are `app_error`; `collector_failure` is only the collector's own parse/IO errors;
- 0.1.7: finding ids stay hashes of project, kind and cause signature, never timestamps, rows or counts;
- 0.1.7: a session's slowest turns over 10 s (top 3) are a `turn_latency` finding;
- 0.1.7: visible island overlays outside the island (2 pt tolerance) or off screen are `island_layout` findings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import re
import time
from datetime import datetime

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
ROLLUP_WINDOW_S = 30
ROLLUP_LEAD_S = 5
TURN_SLOW_MS = 10_000
TURN_TOP_N = 3
TURN_METRICS = ('speech_to_playback_ms', 'turn_ms')
SESSION_STAGES = frozenset({'mic_preflight', 'audio_ready', 'realtime_ws_open', 'realtime_ready',
                            'stack_begin', 'response_done', 'ledger_task', 'task_outcome', 'comet_begin', 'comet_act',
                            'comet_end', 'cua_act', 'cua_state', 'voice_latency', 'usage', 'island_layout',
                            'ledger_select', 'ledger_save_receipt', 'ledger_supersede'})
LAYOUT_TOLERANCE_PT = 2


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _key(parts):
    return 'col-' + hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:16]


def _seconds(ms):
    return f'{ms / 1000:.1f} s' if ms >= 1000 else f'{ms:.0f} ms'


def _label(event):
    return f"{event['kind']}/{event['step']}" if event['step'] else event['kind']


def _test_fix(kind, finding, events):
    if not any(event.get('metric') == 'test_ms' for event in events):
        return None
    if kind == 'failure':
        failed = next((event.get('step') for event in events if event.get('error')), 'the named test')
        return f'Inspect the CI check log for the failed test {failed}.'
    if kind == 'latency_hotspot':
        latency = finding.get('latency', {})
        name = next((event.get('step') for event in events if event.get('step')), 'the named slow test')
        return (f"Profile and shorten {name} or split it (p95 {latency.get('p95_ms', 'unknown')} ms "
                f"over {latency.get('n', 'unknown')} runs).")
    if kind == 'redundancy':
        return 'Deduplicate or cache the repeated test call; if it is a retry, explain its cause.'
    return None


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
    tests = {(e['project'], e['session']) for e in events if e['test'] and e['session'] and e.get('session_attribution') == 'event'}
    versions = defaultdict(set)
    for e in events:
        if e['version'] and e['session'] and e.get('session_attribution') == 'event' and e.get('version_attribution') == 'event':
            versions[(e['project'], e['session'])].add(e['version'])
    kept, excluded = [], Counter()
    for e in events:
        key = (e['project'], e['session'])
        if e.get('session_attribution') == 'event' and key in tests:
            e['test'] = True
        if not e['version'] and e.get('session_attribution') == 'event' and len(versions[key]) == 1:
            e['version'] = next(iter(versions[key]))
            e['version_attribution'] = 'session_inferred'
        if e['test'] and not include_tests:
            excluded[e['source']] += 1
            continue
        kept.append(e)
    nxt = {}
    for e in reversed(kept):
        key = (e['source'], e['session'])
        if e.get('turn') is not None:
            nxt[key] = e['turn']
        e['turn_ctx'] = nxt.get(key)
    return kept, excluded


def _family(event):
    """Subsystem a failure belongs to: a tool family (`ledger_task` -> `ledger`) or a `<family>_*error` kind."""
    kind = event['kind']
    if 'tool' in kind and event['step']:
        return event['step'].split('_')[0]
    match = re.fullmatch(r'([a-z]+)_[a-z_]*error', kind)
    if match and kind != 'tool_error':
        return match.group(1)
    return None


def _cause(event):
    """The cause signature failures cluster on: the normalized message, else kind/step."""
    return signature(event['message']) or f"{event['kind']}/{event['step']}"


def rollup(events):
    """One root cause per incident: a subsystem's errors in the same session and turn within 30 s.
    The root is the internal error, else the first error; the rest are `effects`, and the calls that
    led to the root are absorbed so they are not reported again as redundancy."""
    result = {'effect_of': {}, 'effects': defaultdict(list), 'calls': defaultdict(list), 'absorbed_calls': set()}
    groups = defaultdict(list)
    for e in events:
        if not e['error'] or e['mirror'] or e.get('alias') or e['session'] is None or e['ts'] is None:
            continue
        family = _family(e)
        if family is not None:
            groups[(e['project'], e['session'], e.get('turn_ctx'), family)].append(e)
    tool_starts = defaultdict(list)
    for e in events:
        if e['kind'] == 'tool_start' and e['ts'] is not None and e['step']:
            tool_starts[(e['project'], e['session'], e.get('turn_ctx'))].append(e)
    for (_project, _session, _turn, family), family_rows in groups.items():
        family_rows.sort(key=lambda e: e['ts'])
        runs = [[family_rows[0]]]
        for prev, cur in zip(family_rows, family_rows[1:]):
            if cur['ts'] - prev['ts'] <= ROLLUP_WINDOW_S:
                runs[-1].append(cur)
            else:
                runs.append([cur])
        for run in runs:
            # Same cause signature = same cause (it already shares a cluster); only other causes are effects.
            if len({_cause(e) for e in run}) < 2:
                continue
            root = next((e for e in run if 'tool' not in e['kind']), run[0])
            effects = [e for e in run if _cause(e) != _cause(root)]
            calls = [c for c in tool_starts[(root['project'], root['session'], root.get('turn_ctx'))]
                     if c['step'].split('_')[0] == family and run[0]['ts'] - ROLLUP_LEAD_S <= c['ts'] <= run[-1]['ts']]
            result['effects'][id(root)] = effects
            result['calls'][id(root)] = calls
            result['absorbed_calls'].update(id(c) for c in calls)
            for e in effects:
                result['effect_of'][id(e)] = id(root)
    return result


def _status(ctx, project, last_ts, by_version):
    """Failures: none on the project's latest version -> recovered. Otherwise a recent window decides."""
    latest = ctx.latest.get(project)
    if by_version is not None and latest is not None and not by_version.get(latest) and not by_version.get('unknown'):
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
    plain = [e for e in newest if not e.get('alias')] or newest
    message = plain[-1].get('message') or ''
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
        'event_timeline': [{'event_id': e['ref'], 'event_at': iso(e['ts']) if e['ts'] is not None else None,
                            'event_type': e['kind'], 'outcome': 'failure' if e['error'] else 'unknown',
                            'safe_evidence': {'tool': e['step'], 'version': e.get('version'),
                                              'version_attribution': e.get('version_attribution', 'unknown'),
                                              'session_attribution': e.get('session_attribution', 'unknown'),
                                              'first_error_line': scrub(e.get('message'), 120) or None,
                                              'error_details': e.get('error_details'),
                                              'ledger_result_detail': e.get('ledger_result_detail'),
                                              'ledger_result_status': e.get('ledger_result_status'),
                                              'ledger_decision_count': e.get('ledger_decision_count'),
                                              'task_outcome': e.get('task_outcome'),
                                              'task_check_id': e.get('task_check_id'),
                                              'task_evidence_class': e.get('task_evidence_class'),
                                              'task_tools': e.get('task_tools'), 'run_ref': e.get('run_ref'),
                                              'response_ref': e.get('response_ref')}}
                           for e in list(reversed(plain))[:20]],
        'event_timeline_total': len(plain), 'event_timeline_truncated': len(plain) > 20,
    }
    if by_version and set(by_version) != {'unknown'}:
        finding['by_version'] = dict(by_version)
        latest = ctx.latest.get(project)
        if latest is not None:
            after = by_version.get(latest, 0)
            finding['before_after'] = {'version': latest, 'before': count - after, 'after': after}
    finding.update(extra or {})
    if kind == 'failure' and any(e.get('metric') == 'test_ms' for e in events) and not message:
        finding['what_happened'] = 'A CI test process failed; its failure output was not retained.'
    if test_fix := _test_fix(kind, finding, events):
        finding['fix'] = test_fix
    return finding


def _occurrences(items):
    """Within a source, a session counts the largest number any one kind/step logged for this cause
    (startup step + realtime_error for the same failure = 1). Across sources, the largest source wins
    (voice.log repeating the activity log adds nothing)."""
    best = (0, Counter(), None)
    by_source = defaultdict(list)
    for e in items:
        by_source[e['source']].append(e)
    for rows in by_source.values():
        per_session, version_of = defaultdict(Counter), {}
        for e in rows:
            per_session[e['session']][(e['kind'], e['step'], signature(e['message']))] += 1
            version_of.setdefault(e['session'], e['version'])
        by_version = Counter()
        for session, counts in per_session.items():
            by_version[version_of.get(session) or 'unknown'] += max(counts.values())
        total = sum(by_version.values())
        if total > best[0]:
            best = (total, by_version, rows)
    return best


def failures(ctx, events, roll):
    """Root-cause clusters: (project, cause signature). Each session counts its occurrences once,
    taking the largest count any single source/kind logged for it, so mirrors are not double counted.
    Downstream errors of a rolled-up incident are attached to the root cause, never clustered again."""
    clusters, phase_home = defaultdict(list), {}
    errors = [e for e in events if e['error'] and not e['mirror']]
    effects_of = roll['effect_of']
    for e in errors:
        if e.get('alias') or id(e) in effects_of:
            continue
        # A tool's failure stays with that tool; other kinds (startup step, realtime_error, log line) share a cause.
        tool = e['step'] if 'tool' in e['kind'] else ''
        key = (e['project'], signature(e['message']) or f"{e['kind']}/{e['step']}", tool)
        clusters[key].append(e)
        if e['step'] and e['session'] is not None:
            phase_home.setdefault((e['project'], e['session'], e['step']), key)
    for e in errors:
        if e.get('alias') and id(e) not in effects_of:
            # Prose lines ('mic preflight failed: …') join the structured failure of that phase in the same session.
            home = phase_home.get((e['project'], e['session'], e['step']))
            clusters[home or (e['project'], signature(e['message']) or f"{e['kind']}/{e['step']}", '')].append(e)
    out = []
    for parts, items in clusters.items():
        occurrences, by_version, counted = _occurrences(items)
        logged = Counter()
        for e in items:
            logged[f"{e['source'].split(':')[-1]} {_label(e)}"] += 1
        # Name the cluster after the source that was counted, so a log echo never renames the failure.
        lead = Counter(_label(e) for e in counted or items if e['step']).most_common(1)
        label = lead[0][0] if lead else _label(items[0])
        condition = next((e['condition'] for e in items if e['condition']), None)
        cause = f' ({condition})' if condition else ''
        error_class = 'environment_condition' if condition else 'app_error'
        by_version = dict(by_version)
        extra = {'logged_as': dict(logged.most_common(6)), **({'condition': condition} if condition else {})}
        headline = f"{parts[0]}: {label}{cause} failed {occurrences}x"
        roots = [e for e in items if id(e) in roll['effects']]
        if roots:
            downstream = [x for root in roots for x in roll['effects'][id(root)]]
            calls = [c for root in roots for c in roll['calls'][id(root)]]
            kinds = dict(Counter(_label(x) for x in downstream).most_common(6))
            extra['effects'] = {'count': len(downstream), 'incidents': len(roots), 'calls': len(calls),
                                'retries': sum(max(0, len(roll['calls'][id(root)]) - 1) for root in roots),
                                'kinds': kinds}
            headline += f" (+{len(downstream)} downstream: {', '.join(kinds)})"
        out.append(_finding(ctx, 'failure', error_class, parts, items, headline, occurrences, extra, by_version))
    return out


def collector_failures(ctx, stats):
    """The collector's own parse/IO gaps are one `collector_failure` finding per unreadable source."""
    out = []
    for source, item in stats.items():
        skipped = item.get('skipped', 0) if isinstance(item.get('skipped', 0), int) else 0
        io_errors = item.get('io_errors', 0) if isinstance(item.get('io_errors', 0), int) else 0
        if skipped <= 0 and io_errors <= 0:
            continue
        project = source.split(':')[0]
        synthetic = {'project': project, 'source': source, 'kind': 'collector',
                     'step': 'parse' if skipped else 'io', 'error': True, 'ts': None, 'duration_ms': None,
                     'metric': None, 'message': '', 'ref': source, 'session': None, 'condition': None,
                     'mirror': False, 'test': False, 'version': None}
        out.append(_finding(ctx, 'failure', 'collector_failure', (project, source, 'collector'), [synthetic],
                            f"{project}: collector could not read {source}: {skipped} unparsed row(s), "
                            f"{io_errors} IO error(s)", skipped + io_errors,
                            {'collector': {'skipped': skipped, 'io_errors': io_errors}}))
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


def redundancy(ctx, events, roll):
    """Bursts of identical failures, retries/reconnects and duplicate calls. Metric rows never form bursts.
    A duplicate call needs the same tool and normalized arguments; otherwise the repeat is a retry."""
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
    streams = defaultdict(list)
    for e in events:
        streams[(e['source'], e['session'])].append(e)
    last_call, duplicates, tool_retries = {}, defaultdict(list), defaultdict(list)
    for (source, session), rows in streams.items():
        for index, e in enumerate(rows):
            # One canonical call record per tool use; companion rows (comet_call) would double every call.
            if e['kind'] != 'tool_start' or e['ts'] is None or id(e) in roll['absorbed_calls']:
                continue
            args = e.get('args')
            if args is None:
                for other in rows[index + 1:]:
                    if other['kind'] == 'tool_start':
                        break
                    if (other['kind'].endswith('_call') and other['step'] == e['step'] and other['ts'] is not None
                            and 0 <= other['ts'] - e['ts'] <= 1.0):
                        args = other.get('args')
                        break
            key = (e['project'], source, session, e['step'])
            if key in last_call and e['ts'] - last_call[key][0] <= DUPLICATE_WINDOW_S:
                prev_args = last_call[key][1]
                if args is not None and args == prev_args:
                    duplicates[(e['project'], source, e['step'])].append(e)
                else:
                    tool_retries[(e['project'], source, e['step'])].append(e)
            last_call[key] = (e['ts'], args)
    for parts, items in duplicates.items():
        if len(items) >= 2:
            out.append(_redundancy(ctx, parts + ('duplicate_call',), items, 'duplicate_call', len(items), 0, 'duplicated'))
    for parts, items in tool_retries.items():
        if len(items) >= 2:
            out.append(_redundancy(ctx, (*parts, 'tool_retry'), items, 'retry', len(items), 0,
                                   'retried with different or unknown arguments'))
    return out


def turn_latency(ctx, events):
    """Each session's slowest explicitly numbered turns, with the tools called and errors seen in them."""
    turns = defaultdict(dict)
    for e in events:
        if e.get('turn') is None or e['session'] is None or e['metric'] not in TURN_METRICS or e['duration_ms'] is None:
            continue
        current = turns[(e['project'], e['session'])].get(e['turn'])
        if current is None or e['duration_ms'] > current['duration_ms']:
            turns[(e['project'], e['session'])][e['turn']] = e
    out = []
    for (project, session), session_turns in turns.items():
        slow = sorted(((turn, e) for turn, e in session_turns.items() if e['duration_ms'] >= TURN_SLOW_MS),
                      key=lambda item: item[1]['duration_ms'], reverse=True)
        if not slow:
            continue
        details = []
        for turn, event in slow[:TURN_TOP_N]:
            tools = sorted({x['step'] for x in events if x['kind'] == 'tool_start' and x['session'] == session
                            and x.get('turn_ctx') == turn and x['step']})[:5]
            errors = sum(1 for x in events if x['error'] and not x['mirror'] and x['session'] == session
                         and x.get('turn_ctx') == turn)
            duration = round(event['duration_ms'], 1)
            details.append({'turn': turn, 'ms': int(duration) if duration == int(duration) else duration,
                            'tools': tools, 'errors': errors, 'timestamp': iso(event['ts']) if event['ts'] is not None else None,
                            'version': event.get('version'), 'metric': event.get('metric')})
        headline = (f"{project}: session {session} slowest turn {_seconds(slow[0][1]['duration_ms'])} "
                    f"(turn {slow[0][0]}")
        if details[0]['tools']:
            headline += f", tools {', '.join(details[0]['tools'])}"
        if details[0]['errors']:
            headline += f", {details[0]['errors']} error(s)"
        headline += f"); {len(slow)} turn(s) over {_seconds(TURN_SLOW_MS)}"
        out.append(_finding(ctx, 'turn_latency', 'turn_latency', (project, 'session', session),
                            [e for _turn, e in slow], headline, len(slow),
                            {'turn_latency': {'session': session, 'threshold_ms': TURN_SLOW_MS,
                                              'max_ms': slow[0][1]['duration_ms'], 'slow_turns': len(slow),
                                              'turns': details}}))
    return out


def _contains(outer, rect, tolerance=LAYOUT_TOLERANCE_PT):
    return (rect['x'] >= outer['x'] - tolerance and rect['y'] >= outer['y'] - tolerance
            and rect['x'] + rect['w'] <= outer['x'] + outer['w'] + tolerance
            and rect['y'] + rect['h'] <= outer['y'] + outer['h'] + tolerance)


def _overflow(outer, rect):
    return max(0, outer['x'] - rect['x'], outer['y'] - rect['y'],
               rect['x'] + rect['w'] - (outer['x'] + outer['w']),
               rect['y'] + rect['h'] - (outer['y'] + outer['h']))


def island_layout(ctx, events):
    """Visible island overlays outside the island, and visible rectangles off their screen."""
    groups = defaultdict(list)
    for e in events:
        spec = e.get('layout')
        if not isinstance(spec, dict):
            continue
        screen, island = spec.get('screen'), spec.get('island')
        overlays = spec.get('overlays') or []
        violations = []
        for overlay in overlays:
            if overlay.get('visible') and overlay.get('expected_inside_island') and not _contains(island, overlay):
                violations.append((overlay['kind'], 'outside_island', _overflow(island, overlay), island, overlay))
        rects = ([('island', island)] if island.get('visible') else []) + \
                [(overlay['kind'], overlay) for overlay in overlays if overlay.get('visible')]
        for name, rect in rects:
            if not _contains(screen, rect):
                violations.append((name, 'off_screen', _overflow(screen, rect), screen, rect))
        for name, violation, overflow, outer, rect in violations:
            relative = {'x': round(rect['x'] - outer['x'], 1), 'y': round(rect['y'] - outer['y'], 1),
                        'w': round(rect['w'], 1), 'h': round(rect['h'], 1)}
            bounds = {'w': round(outer['w'], 1), 'h': round(outer['h'], 1)}
            groups[(e['project'], name, violation)].append((e, overflow, spec.get('trigger', ''), bounds, relative))
    out = []
    for (project, name, violation), rows in groups.items():
        items = [row[0] for row in rows]
        max_overflow = max(row[1] for row in rows)
        triggers = dict(sorted(Counter(row[2] for row in rows).items()))
        recent = sorted(rows, key=lambda row: row[0]['ts'] if row[0]['ts'] is not None else -1, reverse=True)[:10]
        occurrences = [{'timestamp': iso(e['ts']) if e['ts'] is not None else None,
                        'version': e.get('version'), 'version_attribution': e.get('version_attribution', 'unknown'), 'trigger': trigger,
                        'overflow_pt': round(overflow, 1), 'bounds': bounds, 'rect': rect}
                       for e, overflow, trigger, bounds, rect in recent]
        headline = (f"{project}: island {name} "
                    f"{'outside the island' if violation == 'outside_island' else 'off screen'} "
                    f"{len(items)}x (max {max_overflow:.1f} pt)")
        out.append(_finding(ctx, 'island_layout', 'layout_violation', (project, 'island_layout', name, violation),
                            items, headline, len(items),
                            {'island_layout': {'overlay_kind': name, 'violation': violation,
                                               'max_overflow_pt': round(max_overflow, 1), 'triggers': triggers,
                                               'occurrences': occurrences}},
                            by_version=Counter(e['version'] or 'unknown' for e in items)))
    return out


def _rank(finding):
    active = finding['status'] == 'active'
    if finding['kind'] == 'latency_hotspot':
        impact = finding.get('latency', {}).get('p95_ms', 0) / 1000
    elif finding['kind'] == 'turn_latency':
        impact = finding.get('turn_latency', {}).get('max_ms', 0) / 1000
    else:
        impact = finding['failed_attempt_count']
    return (active, impact)


def _scope_identity(event):
    version, attribution = event.get('version'), event.get('version_attribution')
    if not isinstance(version, str) or not version or attribution not in ('event', 'session_inferred'):
        return None, 'unknown'
    if scrub(version, 40) != version:
        return None, 'unknown'
    return version, attribution


def _task_records(events, snapshot_id, sidecar_writer):
    groups, gaps = defaultdict(list), Counter()
    gap_sources = defaultdict(Counter)
    for event in events:
        run_ref, step_ref = event.get('run_ref'), event.get('step_ref')
        source = event.get('source') or 'unknown'
        if not isinstance(run_ref, str) or not re.fullmatch(r'[0-9a-f]{24}', run_ref):
            gap_sources[(event.get('project'), source)]['run_ref_missing'] += 1
            if not step_ref:
                gap_sources[(event.get('project'), source)]['legacy_event_unlinked'] += 1
            continue
        task_key = (event.get('project'), run_ref)
        if not isinstance(step_ref, str) or not step_ref:
            groups[task_key].append((None, event))
            gap_sources[(event.get('project'), source)]['step_ref_missing'] += 1
            continue
        groups[task_key].append((step_ref, event))

    index = []
    for (project, run_ref), rows in groups.items():
        rows.sort(key=lambda row: (row[1].get('ts') if row[1].get('ts') is not None else float('inf'),
                                   str(row[1].get('ref') or '')))
        by_step = defaultdict(list)
        for step_ref, event in rows:
            if step_ref:
                data = event.get('task_data') if isinstance(event.get('task_data'), dict) else {}
                by_step[step_ref].append({
                    'event_ref': event.get('ref'), 'timestamp': iso(event.get('ts')),
                    'stage': event.get('step') or event.get('kind'), 'kind': event.get('kind'),
                    'duration_ms': event.get('duration_ms'),
                    'status': 'failure' if event.get('error') else event.get('outcome', 'unknown'),
                    'error_type': data.get('error_type') or (event.get('kind') if event.get('error') else None),
                    'message': event.get('error_details') or event.get('message') or None,
                    'data': data, 'retry_of_step_ref': event.get('retry_of_step_ref'),
                    'retry_of_run_ref': event.get('retry_of_run_ref'), 'mirror_of': event.get('mirror_of'),
                    'caused_by': event.get('caused_by') or [], 'source': scrub(event.get('source'), 80),
                    'ledger': {key: event.get(key) for key in ('ledger_operation', 'ledger_result_status',
                              'ledger_error_category', 'ledger_selected_count') if event.get(key) is not None}})
        steps, details = [], {}
        for step_ref, event_rows in by_step.items():
            event_rows.sort(key=lambda row: (row.get('timestamp') or '', str(row.get('event_ref') or '')))
            failed = next((row for row in event_rows if row['status'] == 'failure'), None)
            last = event_rows[-1]
            status = 'failure' if failed else next(
                (row['status'] for row in reversed(event_rows) if row['status'] in ('success', 'cancelled')), 'unknown')
            merged = {}
            for row in event_rows:
                merged.update(row['data'])
            duration = next((row['duration_ms'] for row in reversed(event_rows)
                             if isinstance(row.get('duration_ms'), (int, float))), None)
            if duration is None:
                times = [datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                         for row in event_rows if row.get('timestamp')]
                duration = round((max(times) - min(times)) * 1000) if len(times) > 1 else None
            step = {'step_ref': step_ref, 'stage': last['stage'],
                    'action': merged.get('action') or last['stage'] or last['kind'],
                    'target': merged.get('target') or merged.get('app'),
                    'duration_ms': duration, 'status': status,
                    'retry_of_step_ref': next((row['retry_of_step_ref'] for row in event_rows if row['retry_of_step_ref']), None),
                    'retry_of_run_ref': next((row['retry_of_run_ref'] for row in event_rows if row['retry_of_run_ref']), None),
                    'mirror_of': next((row['mirror_of'] for row in event_rows if row['mirror_of']), None),
                    'mirror_count': 1, 'mirror_refs': [], 'data': merged,
                    'error_type': failed['error_type'] if failed else None,
                    'message': failed['message'] if failed else None,
                    'caused_by': list(dict.fromkeys(ref for row in event_rows for ref in row['caused_by']))}
            steps.append((event_rows[0].get('timestamp') or '', step))
            details[step_ref] = event_rows
        steps.sort(key=lambda pair: (pair[0], pair[1]['step_ref']))
        step_map = {step['step_ref']: step for _, step in steps}
        for _, step in steps:
            mirror = step.get('mirror_of')
            if isinstance(mirror, str) and mirror in step_map and mirror != step['step_ref']:
                step_map[mirror]['mirror_refs'].append(step['step_ref'])
        visible_steps = []
        for _, step in steps:
            if step['step_ref'] in {ref for current in step_map.values() for ref in current['mirror_refs']}:
                continue
            step['mirror_count'] = 1 + len(step['mirror_refs'])
            visible_steps.append(step)

        all_events = [event for _, event in rows]
        data_rows = [event.get('task_data') for event in all_events if isinstance(event.get('task_data'), dict)]
        request = next((data.get('request') for data in data_rows if isinstance(data.get('request'), str)), None)
        expected = next((data.get('expected_outcome') for data in data_rows if data.get('expected_outcome') is not None), 'unknown')
        outcome_event = next((event for event in reversed(all_events) if event.get('kind') == 'task_outcome'), {})
        explicit_outcome = next((event.get('task_outcome') for event in reversed(all_events)
                                 if event.get('task_outcome')), None)
        first_failure = next((step for step in visible_steps if step['status'] == 'failure'), None)
        observed = explicit_outcome or ('failure' if first_failure else 'unknown')
        typed_build = next((event.get('build') for event in reversed(all_events) if event.get('build')), None)
        version_event = next((event for event in reversed(all_events) if event.get('version')), None)
        first_ts = next((event.get('ts') for event in all_events if event.get('ts') is not None), None)
        last_ts = next((event.get('ts') for event in reversed(all_events) if event.get('ts') is not None), None)
        duration = max(0, int((last_ts - first_ts) * 1000)) if first_ts is not None and last_ts is not None else None
        task_gaps = []
        if request is None:
            task_gaps.append({'code': 'request_missing', 'next_action': 'Emit request on a run event.'})
        if expected == 'unknown':
            task_gaps.append({'code': 'outcome_expected_missing', 'next_action': 'Emit expected_outcome for this run.'})
        missing_steps = [event for step_ref, event in rows if step_ref is None]
        if missing_steps:
            task_gaps.append({'code': 'step_ref_missing', 'producer': scrub(missing_steps[0].get('source'), 80),
                              'count': len(missing_steps), 'next_action': 'Emit a stable step_ref for each task event.'})
        if any('mic' in str(event.get('step') or event.get('kind')).lower() for event in all_events):
            task_gaps.append({'code': 'mic_state_not_in_scope',
                              'next_action': 'Inspect microphone readiness in the owning runtime.'})
        direct_inputs = {}
        if first_failure:
            for input_ref in first_failure['caused_by']:
                if input_ref in details:
                    direct_inputs[input_ref] = [row['data'] for row in details[input_ref]]
                else:
                    task_gaps.append({'code': 'caused_by_ref_missing', 'step_ref': first_failure['step_ref'],
                                      'input_ref': input_ref, 'next_action': 'Emit an explicit caused_by input step_ref.'})
        ledger_rows = [(event, event.get('task_data') or {}) for event in all_events
                       if event.get('ledger_operation') or event.get('step') == 'ledger_task'
                       or 'ledger' in str(event.get('kind', '')).lower()]
        ledger = None
        if ledger_rows:
            event, ledger_data = next(((event, data) for event, data in ledger_rows
                                       if data.get('query_terms') or event.get('ledger_result_status')), ledger_rows[0])
            ledger = {'lookup_status': event.get('ledger_result_status') or 'unknown',
                      'query_terms': ledger_data.get('query_terms'),
                      'invalid_query_terms': ledger_data.get('invalid_query_terms'),
                      'selected_id': ledger_data.get('selected_id') or 'none',
                      'operation': event.get('ledger_operation'),
                      'decision_count': event.get('ledger_decision_count')}
            use = next((event for event in all_events if event.get('kind') in ('ledger_use', 'ledger_record_used')),
                       None)
            if use:
                ledger['applied'] = True
        cancellation = {key: data[key] for data in data_rows for key in (
            'cancel', 'cancel_ack', 'cancel_state', 'target_run_ref', 'queued_action',
            'queued_action_suppressed') if key in data}
        project_rows = [event for event in all_events if event.get('project') == project]
        source = scrub(project_rows[0].get('source'), 80) if project_rows else 'unknown'
        task = {'snapshot_id': snapshot_id, 'project': project, 'run_ref': run_ref,
                'request': request, 'build': typed_build,
                'version': version_event.get('version') if version_event else None,
                'version_attribution': version_event.get('version_attribution', 'unknown') if version_event else 'unknown',
                'outcome': {'expected': expected, 'observed': observed,
                            **({'check_id': outcome_event['task_check_id']} if outcome_event.get('task_check_id') else {}),
                            **({'evidence_class': outcome_event['task_evidence_class']}
                               if outcome_event.get('task_evidence_class') else {}),
                            **{key: outcome_event['task_data'][key] for key in ('verified', 'completion')
                               if isinstance(outcome_event.get('task_data'), dict) and
                               key in outcome_event['task_data']},
                            **({'next_action': 'Emit expected_outcome and an explicit task_outcome event.'}
                               if expected == 'unknown' or observed == 'unknown' else {})},
                'steps': visible_steps, 'step_details': details, 'first_failure': first_failure,
                'direct_inputs': direct_inputs, 'ledger': ledger, 'gaps': task_gaps,
                'cancellation': cancellation or None,
                'generated_at': iso(last_ts) if last_ts is not None else None}
        file_key = hashlib.sha256((str(project) + '\0' + run_ref).encode()).hexdigest()[:24]
        file_ref = sidecar_writer('tasks', file_key, task)
        request_summary = request or ''
        if len(request_summary) > 120:
            request_summary = request_summary[:117] + '...'
        first_error_type = first_failure.get('error_type') if first_failure else None
        index_outcome = ('success' if observed in ('success', 'verified_success') else
                         'failure' if observed in ('failure', 'verified_incomplete') else
                         'cancelled' if observed == 'cancelled' else 'unknown')
        index.append({'project': project, 'run_ref': run_ref, 'request': request_summary,
                      'outcome': index_outcome, 'first_error_type': first_error_type, 'duration_ms': duration,
                      'step_count': len(by_step), 'last_seen': iso(last_ts) if last_ts is not None else None,
                      'source': source, 'file': file_ref})
    index.sort(key=lambda item: (item.get('last_seen') or '', item['run_ref']), reverse=True)
    index.sort(key=lambda item: item.get('first_error_type') is None)
    for (project, source), counts in gap_sources.items():
        for code, count in counts.items():
            gaps[(project, source, code)] += count
    gap_index = [{'project': project, 'producer': scrub(source, 80), 'code': code, 'count': count,
                  'next_action': ('Emit run_id so events can be linked to a task.' if code == 'run_ref_missing'
                                  else 'Emit step_ref on task events.' if code == 'step_ref_missing'
                                  else 'Emit run_id and step_ref so legacy events can be linked.')}
                 for (project, source, code), count in sorted(gaps.items())]
    return index, gap_index


def build(events, stats, now=None, include_tests=False, window_h=ACTIVE_WINDOW_H, threshold_ms=HOTSPOT_MS,
          test_threshold_ms=TEST_HOTSPOT_MS, *, scoped=True, snapshot_id=None, sidecar_writer=None,
          include_sessions=True):
    now = time.time() if now is None else now
    events, excluded = prepare(events, include_tests)
    roll = rollup(events)
    ctx = Context(events, now, window_h, threshold_ms, test_threshold_ms)
    findings = (failures(ctx, events, roll) + collector_failures(ctx, stats) + latency(ctx, events)
                + turn_latency(ctx, events) + redundancy(ctx, events, roll) + island_layout(ctx, events))
    findings.sort(key=_rank, reverse=True)
    for rank, finding in enumerate(findings, 1):
        finding['rank'] = rank
    for source, count in excluded.items():
        stats.setdefault(source, {})['excluded_test'] = count
    stamp = iso(now)
    session_groups = defaultdict(list)
    if include_sessions:
        for event in events:
            if not event.get('session') or event.get('session_attribution') != 'event' or event.get('ts') is None:
                continue
            stage = event.get('step') if event.get('step') in SESSION_STAGES else event.get('kind')
            if stage not in SESSION_STAGES:
                continue
            session_groups[(event['project'], event['session'])].append((event, stage))
    compact = bool(sidecar_writer)
    session_timelines = []
    for (project, session), rows in session_groups.items():
        rows.sort(key=lambda row: row[0]['ts'])
        session_ref = 'sref_' + hashlib.sha256((project + '\0' + session).encode()).hexdigest()[:32]
        timeline = [{'timestamp': iso(event['ts']), 'stage': stage,
                     'duration_ms': event.get('duration_ms'), 'metric': event.get('metric'),
                     'outcome': event.get('outcome') or ('failure' if event['error'] else 'unknown'),
                     'version': event.get('version'),
                     'version_attribution': event.get('version_attribution') or 'unknown',
                     'ledger_operation': event.get('ledger_operation'),
                     'ledger_scope_kind': event.get('ledger_scope_kind'),
                     'ledger_rule_supplied': event.get('ledger_rule_supplied'),
                     'ledger_error_category': event.get('ledger_error_category'),
                     'ledger_result_status': event.get('ledger_result_status'),
                     'ledger_decision_count': event.get('ledger_decision_count'),
                     'ledger_result_detail': event.get('ledger_result_detail'),
                     'ledger_detail_truncated': event.get('ledger_detail_truncated'),
                     'input_availability': event.get('input_availability'), 'input_device': event.get('input_device'),
                     'input_rate': event.get('input_rate'), 'input_recovery': event.get('input_recovery'),
                     'input_selection': event.get('input_selection'),
                     'layout': event.get('layout'),
                     'task_outcome': event.get('task_outcome'), 'task_check_id': event.get('task_check_id'),
                     'task_evidence_class': event.get('task_evidence_class'), 'task_tools': event.get('task_tools'),
                     'error_details': event.get('error_details'), 'run_ref': event.get('run_ref'),
                     'call_ref': event.get('call_ref'), 'response_ref': event.get('response_ref'),
                     'mirror_ref': event.get('mirror_ref'), 'source': event.get('source'), 'source_ref': event.get('ref'),
                     'event_kind': event.get('kind'), 'build': event.get('build'),
                     'ledger_rule': event.get('ledger_rule'),
                     'ledger_selected_count': event.get('ledger_selected_count'),
                     'ledger_selected_id': event.get('ledger_selected_id'),
                     'ledger_supersedes': event.get('ledger_supersedes'),
                     'ledger_decision_id': event.get('ledger_decision_id'),
                     'ledger_revision': event.get('ledger_revision')}
                    for event, stage in rows]
        # Collapse only records that explicitly carry the same producer-issued identity.
        grouped = {}
        collapsed = []
        for item in timeline:
            key = (item.get('mirror_ref'), item['stage']) if item.get('mirror_ref') else None
            previous = grouped.get(key) if key else None
            if previous and previous['source'] != item['source']:
                if previous['event_kind'] in ('log', 'log_error') and item['event_kind'] not in ('log', 'log_error'):
                    collapsed[collapsed.index(previous)] = item
                    grouped[key] = item
                    previous, item = item, previous
                previous.setdefault('mirrors', []).append({'timestamp': item['timestamp'], 'source': item['source'], 'ref': item['source_ref']})
            else:
                collapsed.append(item)
                if key:
                    grouped[key] = item
        timeline = collapsed
        session = {'project': project, 'session_ref': session_ref,
                   'last_seen': iso(rows[-1][0]['ts']), 'events': timeline,
                   'total': len(timeline), 'raw_total': len(rows), 'truncated': False}
        if compact:
            scope_counts, scope_raw_counts, scope_last_seen = Counter(), Counter(), {}
            for item in timeline:
                scope = _scope_identity({'version': item.get('version'),
                                         'version_attribution': item.get('version_attribution')})
                scope_counts[scope] += 1
                scope_last_seen[scope] = item['timestamp']
            for event, _ in rows:
                scope = _scope_identity(event)
                scope_raw_counts[scope] += 1
            file_ref = sidecar_writer('sessions', session_ref, {'snapshot_id': snapshot_id, 'session': session})
            session_timelines.append({'project': project, 'session_ref': session_ref,
                                      'last_seen': session['last_seen'], 'total': session['total'],
                                      'raw_total': session['raw_total'], 'truncated': False, 'file': file_ref,
                                      'scope_counts': scope_counts, 'scope_raw_counts': scope_raw_counts,
                                      'scope_last_seen': scope_last_seen})
        else:
            session_timelines.append(session)
    session_timelines.sort(key=lambda row: row['last_seen'], reverse=True)
    coverage, coverage_details, coverage_gaps = {}, {}, []
    for source, item in stats.items():
        last_event_at = item.get('last_event_at')
        try:
            age = max(0, int(now - datetime.fromisoformat(last_event_at.replace('Z', '+00:00')).timestamp()))
        except (ValueError, TypeError, AttributeError):
            age = None
        stale_after_s = item.get('stale_after_s', 86400)
        if item.get('missing'):
            status = 'missing'
        elif age is not None and age > stale_after_s:
            status = 'stale'
        elif item.get('read') and not item.get('io_errors') and not item.get('skipped'):
            status = 'collected'
        else:
            status = 'stale'
        coverage[source] = status
        detail = {'status': status, 'collector_heartbeat_at': stamp,
                  'last_event_at': last_event_at or 'unknown', 'last_received_at': stamp,
                  'dropped_rows': item.get('skipped', 0), 'read_in_collect': item.get('read') is True,
                  'age_seconds': age, 'stale_after_s': stale_after_s}
        coverage_details[source] = detail
        if status != 'collected':
            coverage_gaps.append({'source': source, 'status': status,
                                  **({'age_seconds': age} if age is not None else {})})
    document = {
        'schema_version': 1, 'generated_at': stamp, 'producer': 'developer-observability-collector',
        'collector_version': VERSION, 'findings': findings, 'session_timelines': session_timelines[:50],
        'settings': {'include_tests': include_tests, 'active_window_h': window_h, 'latency_threshold_ms': threshold_ms,
                     'test_latency_threshold_ms': test_threshold_ms, 'turn_latency_threshold_ms': TURN_SLOW_MS},
        'latest_versions': dict(ctx.latest),
        'coverage': coverage, 'coverage_details': coverage_details,
        'coverage_gaps': coverage_gaps, 'coverage_gap_scope': 'collector', 'collector_stats': stats,
    }
    if scoped:
        groups = defaultdict(list)
        for event in events:
            version = event.get('version')
            attribution = event.get('version_attribution')
            if not isinstance(version, str) or not version or attribution not in ('event', 'session_inferred'):
                version, attribution = None, 'unknown'
            elif scrub(version, 40) != version:
                version, attribution = None, 'unknown'
            groups[(event['project'], version, attribution)].append(
                {**event, 'version': None, 'version_attribution': 'unknown'} if attribution == 'unknown' else event)
        if len(groups) > 256:
            raise ValueError('collector_failed: too many project/version scopes; narrow collector sources')
        document['version_scopes'] = []
        for (project, version, attribution), rows in sorted(groups.items(), key=lambda item: str(item[0])):
            scope_id = hashlib.sha256(json.dumps([project, version, attribution], separators=(',', ':')).encode()).hexdigest()[:24]
            projection = build(rows, {}, now, True, window_h, threshold_ms, test_threshold_ms,
                               scoped=False, include_sessions=not bool(sidecar_writer))
            for finding in projection['findings']:
                finding['finding_key'] = 'col-' + hashlib.sha256((scope_id + '\0' + finding['finding_key']).encode()).hexdigest()[:16]
            if sidecar_writer:
                scoped_sessions = []
                for session in session_timelines:
                    if session['project'] != project:
                        continue
                    scope_key = (version, attribution)
                    total = session.get('scope_counts', {}).get(scope_key, 0)
                    if not total:
                        continue
                    scoped_ref = 'sref_' + hashlib.sha256((scope_id + '\0' + session['session_ref']).encode()).hexdigest()[:32]
                    scoped_sessions.append({'project': project, 'session_ref': scoped_ref,
                                            'global_session_ref': session['session_ref'],
                                            'last_seen': session.get('scope_last_seen', {}).get(scope_key, session['last_seen']),
                                            'total': total,
                                            'raw_total': session.get('scope_raw_counts', {}).get(scope_key, total),
                                            'truncated': False, 'file': session['file']})
                scoped_sessions.sort(key=lambda row: row['last_seen'], reverse=True)
                scope_file = sidecar_writer('scopes', scope_id, {
                    'snapshot_id': snapshot_id, 'findings': projection['findings'], 'sessions': scoped_sessions[:50]})
                document['version_scopes'].append({'project': project, 'version': version, 'attribution': attribution,
                                                   'scope_ref': scope_id, 'finding_count': len(projection['findings']),
                                                   'session_count': len(scoped_sessions),
                                                   'last_seen': scoped_sessions[0]['last_seen'] if scoped_sessions else None,
                                                   'file': scope_file})
            else:
                for session in projection['session_timelines']:
                    session['session_ref'] = 'sref_' + hashlib.sha256((scope_id + '\0' + session['session_ref']).encode()).hexdigest()[:32]
                document['version_scopes'].append({'project': project, 'version': version, 'attribution': attribution,
                                                   'findings': projection['findings'],
                                                   'session_timelines': projection['session_timelines']})
    if sidecar_writer:
        document['snapshot_id'] = snapshot_id
        document['session_index'] = [
            {'project': session['project'], 'session_ref': session['session_ref'], 'last_seen': session['last_seen'],
             'total': session['total'], 'raw_total': session['raw_total'], 'truncated': session['truncated'],
             'file': f'snapshots/{snapshot_id}/sessions/{session["session_ref"]}.json'}
            for session in session_timelines[:50]]
        document.pop('session_timelines', None)
        document['task_index'], document['task_gaps'] = _task_records(events, snapshot_id, sidecar_writer)
    return document
