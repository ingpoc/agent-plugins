import json
import statistics
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/oc-bcl-v1/runs')
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else '/tmp/oc-bcl-v1/results.json')
PLAN = json.loads((Path(__file__).resolve().parent / 'plan.json').read_text())

manifests = []
for path in sorted(ROOT.glob('*/*/manifest.json')):
    manifests.append(json.loads(path.read_text()))

pairs = {}
for item in manifests:
    pairs.setdefault((item['case'], item['rep']), {})[item['arm']] = item

rows = []
for (case, rep), arms in sorted(pairs.items()):
    base, cand = arms.get('baseline'), arms.get('candidate')
    if not base or not cand:
        rows.append({'case': case, 'rep': rep, 'complete': False})
        continue
    both = base['accepted'] and cand['accepted']
    rows.append({
        'case': case,
        'rep': rep,
        'complete': True,
        'both_accepted': both,
        'baseline': {k: base[k] for k in ('accepted', 'shell_calls', 'agent_seconds', 'end_to_end_seconds', 'direct_measurement_eligible', 'candidate_command_used', 'batch_stage_used')},
        'candidate': {k: cand[k] for k in ('accepted', 'shell_calls', 'agent_seconds', 'end_to_end_seconds', 'direct_measurement_eligible', 'candidate_command_used', 'batch_stage_used')},
        'baseline_tokens': base['tokens'],
        'candidate_tokens': cand['tokens'],
        'delta': {
            'shell_calls': cand['shell_calls'] - base['shell_calls'],
            'agent_seconds': round(cand['agent_seconds'] - base['agent_seconds'], 3),
            'uncached_input': cand['tokens']['input_uncached'] - base['tokens']['input_uncached'],
            'output': cand['tokens']['output'] - base['tokens']['output'],
        },
    })

accepted_pairs = [row for row in rows if row.get('both_accepted')]
baseline_rows = [row['baseline'] for row in accepted_pairs]
candidate_rows = [row['candidate'] for row in accepted_pairs]
baseline_tokens = [row['baseline_tokens'] for row in accepted_pairs]
candidate_tokens = [row['candidate_tokens'] for row in accepted_pairs]

def median(values):
    return round(statistics.median(values), 3) if values else None

summary = {
    'model': PLAN['model'],
    'variant': PLAN['variant'],
    'pairs_total': len(rows),
    'pairs_both_accepted': len(accepted_pairs),
    'candidate_accepted': sum(1 for row in rows if row.get('complete') and row['candidate']['accepted']),
    'baseline_accepted': sum(1 for row in rows if row.get('complete') and row['baseline']['accepted']),
    'candidate_stage_used': sum(1 for row in rows if row.get('complete') and row['candidate']['candidate_command_used']),
    'candidate_runs': sum(1 for row in rows if row.get('complete')),
    'medians': {
        'baseline': {
            'shell_calls': median([row['shell_calls'] for row in baseline_rows]),
            'agent_seconds': median([row['agent_seconds'] for row in baseline_rows]),
            'uncached_input': median([row['input_uncached'] for row in baseline_tokens]),
            'output': median([row['output'] for row in baseline_tokens]),
            'cache_read': median([row['cache_read'] for row in baseline_tokens]),
        },
        'candidate': {
            'shell_calls': median([row['shell_calls'] for row in candidate_rows]),
            'agent_seconds': median([row['agent_seconds'] for row in candidate_rows]),
            'uncached_input': median([row['input_uncached'] for row in candidate_tokens]),
            'output': median([row['output'] for row in candidate_tokens]),
            'cache_read': median([row['cache_read'] for row in candidate_tokens]),
        },
    },
    'rows': rows,
}

rule = PLAN['decision_rule']
checks = {
    'adoption': summary['candidate_stage_used'] >= rule['adoption_min_runs'],
    'correctness': all(row.get('both_accepted') for row in rows if row.get('complete'))
                   and summary['candidate_accepted'] == summary['pairs_total'],
    'shell_calls': None,
    'time': None,
    'uncached_input': None,
}
base_med, cand_med = summary['medians']['baseline'], summary['medians']['candidate']
if accepted_pairs:
    checks['shell_calls'] = cand_med['shell_calls'] < base_med['shell_calls']
    checks['time'] = cand_med['agent_seconds'] <= base_med['agent_seconds']
    checks['uncached_input'] = cand_med['uncached_input'] <= base_med['uncached_input'] * rule['uncached_input_max_ratio']
if checks['adoption'] and checks['correctness'] and checks['shell_calls'] and checks['time'] and checks['uncached_input']:
    verdict = 'adopt-for-broader-validation'
elif not checks['correctness'] or summary['candidate_stage_used'] < rule['reject_adoption_below']:
    verdict = 'reject'
else:
    verdict = 'inconclusive'
summary['checks'] = checks
summary['verdict'] = verdict
summary['note'] = rule['note']
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(summary, indent=2, sort_keys=True))
print(json.dumps({k: summary[k] for k in ('model', 'variant', 'pairs_total', 'pairs_both_accepted',
                                          'candidate_accepted', 'baseline_accepted', 'candidate_stage_used',
                                          'candidate_runs', 'medians', 'checks', 'verdict')}, indent=2))
