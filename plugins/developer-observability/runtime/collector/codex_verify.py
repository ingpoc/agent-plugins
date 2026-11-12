"""Run a separate artifact check and retain only its digests and result."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def verify(database, task_id, scenario, skill, artifact, verifier):
    task_id = str(uuid.UUID(task_id))
    database = Path(database).expanduser()
    if not database.is_file():
        raise ValueError('task not found')
    with sqlite3.connect(database) as db:
        if not db.execute('SELECT 1 FROM task_usage WHERE task_id = ?', (task_id,)).fetchone():
            raise ValueError('task not found')
        values = [digest(p) for p in (scenario, skill, artifact, verifier)]
        if values[3] in values[:3]:
            raise ValueError('verifier must be a separate file')
        try:
            result = subprocess.run([str(Path(verifier).resolve()), str(Path(artifact).resolve())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError('verifier did not complete') from error
        if values != [digest(p) for p in (scenario, skill, artifact, verifier)]:
            raise ValueError('verification inputs changed during check')
        receipt = dict(zip(('scenario_sha256', 'skill_sha256', 'artifact_sha256', 'verifier_sha256'), values))
        receipt.update(result='pass' if result.returncode == 0 else 'fail', independence='separate_process')
        payload = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
        db.execute('CREATE TABLE IF NOT EXISTS task_verification (task_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)')
        previous = db.execute('SELECT receipt FROM task_verification WHERE task_id = ?', (task_id,)).fetchone()
        if previous and previous[0] != payload:
            raise ValueError('task already has a different verification')
        db.execute('INSERT OR IGNORE INTO task_verification VALUES (?, ?)', (task_id, payload))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='~/.local/share/developer-observability/task-usage.sqlite3')
    for name in ('task-id', 'scenario', 'skill', 'artifact', 'verifier'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.database, args.task_id, args.scenario, args.skill, args.artifact, args.verifier)
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(1, 'developer-observability: verification failed\n')
    print(json.dumps(result))
    return 0 if result['result'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
