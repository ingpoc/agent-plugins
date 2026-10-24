"""Owner-run local binding; paths and metadata only, no telemetry contents."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/developer-observability/config.json')
    args = parser.parse_args()
    root = args.backend_root.expanduser().resolve()
    if not (root/'telemetry/observability_api.py').is_file():
        parser.error('backend missing telemetry/observability_api.py')
    if args.config.exists():
        parser.error('config exists; preserve and edit the owner binding explicitly')
    config = {'backend_root': str(root), 'runtime': str(Path.home()/'Library/Application Support/KindleSnapshot/runtime'),
              'decider_script': str(Path.home()/'.agents/skills/mac-decider-orchestration/scripts/decide.py'),
              'projects': {'agent-plugins': {'label': 'Agent Plugins', 'labels': ['agent-plugins']},
                           'samantha': {'label': 'Samantha', 'owners': ['owner:samantha'], 'labels': []},
                           'kindle-project': {'label': 'Kindle observability', 'owners': ['owner:kindle-project'], 'labels': ['kindle-project']},
                           'context-ledger': {'label': 'Context Ledger', 'labels': ['context-ledger'], 'recommendation_target': 'context-ledger'}}}
    args.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(config, output, indent=2)
    print('Configured local observability binding; configured labels are provisional attribution.')


if __name__ == '__main__':
    main()
