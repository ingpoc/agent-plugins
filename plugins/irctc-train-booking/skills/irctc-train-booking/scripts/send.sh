#!/bin/bash
# send.sh <actions.json>  — one comet-control send via --payload-file; prints compact result. Needs WORK=/tmp/comet-control-<session>.
set -euo pipefail
: "${WORK:?set WORK to the durable lease workdir}"
[ -s "$1" ] || { echo "FAIL: payload file $1 missing/empty" >&2; exit 2; }
mkdir -p /tmp/irctc
cd ~/.agents/plugins/comet-control
python3 skills/comet-control/scripts/durable_lease_controller.py send --timeout 90 --workdir "$WORK" --payload-file "$1" > /tmp/irctc/last.json
python3 - <<'PY'
import json,sys
d=json.load(open('/tmp/irctc/last.json')); r=d.get('response',d)
print('ok',r.get('success'),r.get('error_code'),(r.get('error') or '')[:200],r.get('final_url'))
if not r.get('success'): print('FAILED_ACTION',json.dumps((r.get('failure_record') or {}).get('action')))
for x in r.get('results',[]):
    t=x.get('type')
    if t=='evaluate': print('EVAL',json.dumps(x.get('result'))[:1500])
    elif t=='page_context': print('INPUTS',json.dumps(x.get('inputs'))[:800])
    elif t=='screenshot': print('SHOT',x.get('screenshot_path'))
sys.exit(0 if r.get('success') else 1)
PY
