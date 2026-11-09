import hashlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get('OCB_ROOT', '/tmp/oc-bcl-v1/runs'))
SOURCE = HOME / 'code/worktrees/.agents'
MODEL = os.environ.get('OCB_MODEL', 'opencode/deepseek-v4.1-flash-free')
VARIANT = os.environ.get('OCB_VARIANT', 'low')
SKILLS = {
    'baseline': SOURCE / 'bcl-status-experiment/skills/batched-change-list',
    'candidate': SOURCE / 'bcl-input-experiment/skills/batched-change-list',
}
CASES = {
    'triage': ['collect', 'present', None],
    'fix': ['plan_fix', 'verify', None],
    'handoff': ['verify', 'report_rollover', None],
}
LABELS = {
    'collect': 'Collect',
    'present': 'Present',
    'plan_fix': 'Plan & fix',
    'verify': 'Verify',
    'report_rollover': 'Report & roll over',
}
STAGE_RE = re.compile(r"manage_findings\.py['\"]?\s+stage\b")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(path):
    digest = hashlib.sha256()
    for file in sorted(x for x in path.rglob('*') if x.is_file() and '__pycache__' not in x.parts and not x.name.endswith('.pyc')):
        digest.update(str(file.relative_to(path)).encode() + b'\0' + bytes.fromhex(sha(file)))
    return digest.hexdigest()


def execute(cmd, cwd, out, err, timeout):
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=err, start_new_session=True)
    except OSError as exc:
        return {'state': 'launch_error', 'error': type(exc).__name__, 'exit_code': None}
    try:
        return {'state': 'exited', 'exit_code': proc.wait(timeout=timeout)}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return {'state': 'timeout', 'exit_code': None, 'process_group': 'kill_sent'}


def run(arm, case, rep, sequence='0', pair_order=''):
    start = time.monotonic()
    skill = SKILLS[arm]
    folder = ROOT / f'{case}-{rep}' / arm
    folder.mkdir(parents=True, exist_ok=False)
    fixture = ROOT.parent / 'fixture.json'
    data = json.loads(fixture.read_text())
    (folder / 'findings.json').write_bytes(fixture.read_bytes())
    task = str(uuid.uuid4())
    stages = CASES[case]
    before = tree(skill)
    prompt = (
        f'Use only the batched-change-list skill at {skill}; read SKILL.md and references/tracker-operations.md. '
        f'For the disposable tracker {folder}/findings.json, perform each stage transition in order: {json.dumps(stages)}. '
        f'For transition N starting at 1, render to {folder}/report-N.html and immediately copy the tracker to {folder}/snapshot-N.json. '
        'Preserve all findings, do not create subagents or change skill files, and touch no other tracker.'
    )
    (folder / 'prompt.txt').write_text(prompt)
    model_ref = f'{MODEL}#{VARIANT}' if VARIANT else MODEL
    cmd = ['opencode', 'run', '--model', model_ref, '--format', 'json', '--auto',
           '--title', f'bcl-{case}-{rep}-{arm}-{task}', prompt]
    started = time.monotonic()
    with (folder / 'events.jsonl').open('w') as out, (folder / 'stderr').open('w') as err:
        execution = execute(cmd, folder, out, err, 240)
    task_elapsed = time.monotonic() - started

    events = []
    for line in (folder / 'events.jsonl').read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    commands = []
    tokens = {'input_uncached': 0, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'reasoning': 0, 'steps': 0}
    session_ids = set()
    for event in events:
        part = event.get('part') or {}
        if part.get('type') == 'tool' and part.get('tool') == 'shell':
            commands.append((part.get('state') or {}).get('input', {}).get('command', ''))
        if event.get('type') == 'step_finish':
            tokens['steps'] += 1
            values = part.get('tokens') or {}
            tokens['input_uncached'] += values.get('input', 0) or 0
            tokens['output'] += values.get('output', 0) or 0
            tokens['reasoning'] += values.get('reasoning', 0) or 0
            cache = values.get('cache') or {}
            tokens['cache_read'] += cache.get('read', 0) or 0
            tokens['cache_write'] += cache.get('write', 0) or 0
        if event.get('sessionID'):
            session_ids.add(event['sessionID'])
    tokens['input_total'] = tokens['input_uncached'] + tokens['cache_read'] + tokens['cache_write']
    stage_used = any(STAGE_RE.search(command or '') for command in commands)
    stage_commands = [c for c in commands if STAGE_RE.search(c or '')]
    batch_stage = any(re.search(r'manage_findings\.py["\']?\s+batch\b', c or '') and 'set-stage' in (c or '') for c in commands)

    checks = []
    try:
        for number, stage in enumerate(stages, 1):
            snapshot = folder / f'snapshot-{number}.json'
            report = folder / f'report-{number}.html'
            if not snapshot.is_file() or not report.is_file():
                checks.append({'step': number, 'pass': False, 'reason': 'missing_artifact'})
                continue
            obj = json.loads(snapshot.read_text())
            text = report.read_text()
            marker = '<script type="application/json" id="findings-data">'
            embedded = json.loads(text.split(marker, 1)[1].split('</script>', 1)[0])
            active = re.findall(r'<li class="workflow-step active" aria-current="step"><span>\d+</span>(.*?)</li>', text)
            correct_stage = (not active if stage is None else len(active) == 1 and html.unescape(active[0]) == LABELS[stage])
            ok = (obj['findings'] == data['findings']
                  and [x['id'] for x in embedded] == [x['id'] for x in data['findings']]
                  and obj.get('tracking', {}).get('workflow', {}).get('stage') == stage
                  and correct_stage)
            checks.append({'step': number, 'pass': ok, 'snapshot_sha256': sha(snapshot), 'report_sha256': sha(report)})
        final = json.loads((folder / 'findings.json').read_text())
        last = json.loads((folder / f'snapshot-{len(stages)}.json').read_text())
        final_ok = final == last
        verify_state = 'completed'
    except Exception as exc:
        verify_state = 'error'
        final_ok = False
        checks.append({'pass': False, 'error': type(exc).__name__})
    receipt = {
        'verifier': 'arm-neutral-json-html-v3',
        'state': verify_state,
        'checks': checks,
        'final_matches_snapshot': final_ok,
        'skill_unchanged': tree(skill) == before,
        'limits': ['no independent chronology proof', 'does not check outside-tracker writes'],
    }
    (folder / 'acceptance.json').write_text(json.dumps(receipt, indent=2, sort_keys=True))
    eligible = bool(execution.get('exit_code') == 0 and tokens['steps'] > 0 and session_ids
                    and all(type(tokens[key]) is int for key in ('input_uncached', 'cache_read', 'cache_write', 'output')))
    result = {
        'arm': arm,
        'case': case,
        'rep': rep,
        'sequence': sequence,
        'pair_order': pair_order,
        'task_id': task,
        'model': MODEL,
        'variant': VARIANT,
        'fixture_sha256': sha(fixture),
        'skill_sha256': before,
        'prompt_sha256': sha(folder / 'prompt.txt'),
        'runner_sha256': sha(__file__),
        'execution': execution,
        'session_ids': sorted(session_ids),
        'task_seconds': round(task_elapsed, 3),
        'end_to_end_seconds': round(time.monotonic() - start, 3),
        'agent_seconds': round(task_elapsed, 3),
        'shell_calls': len(commands),
        'candidate_command_used': stage_used,
        'stage_command_count': len(stage_commands),
        'batch_stage_used': batch_stage,
        'tokens': tokens,
        'acceptance_receipt_sha256': sha(folder / 'acceptance.json'),
        'accepted': (execution.get('exit_code') == 0 and verify_state == 'completed'
                     and all(x['pass'] for x in checks) and len(checks) == len(stages)
                     and final_ok and tree(skill) == before),
        'direct_measurement_eligible': eligible,
        'usage_coverage': 'direct OpenCode CLI process only; verifier and orchestration tokens unknown',
    }
    (folder / 'manifest.json').write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps({k: result[k] for k in ('arm', 'case', 'rep', 'execution', 'accepted',
                                             'direct_measurement_eligible', 'candidate_command_used',
                                             'shell_calls', 'agent_seconds', 'tokens')}), flush=True)


if __name__ == '__main__':
    run(*sys.argv[1:6])
