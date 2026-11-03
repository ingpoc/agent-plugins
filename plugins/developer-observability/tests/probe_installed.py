"""Explicit live acceptance probe; never collected as a unit test or telemetry."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def probe(launcher, output, decision):
    config = json.loads((Path.home()/'.config/developer-observability/config.json').read_text())
    runtime = Path(config['runtime'])
    tracked = [runtime/'recommendations.json']
    before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked if p.exists()}
    params = StdioServerParameters(command=str(launcher))
    proof = {'installed_launcher':str(launcher), 'tools':[], 'decision_tested':False}
    started = time.monotonic()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read,write,extensions={'io.modelcontextprotocol/ui':{'mimeTypes':['text/html;profile=mcp-app']}}) as client:
            await client.discover()
            proof['protocol'] = client.protocol_version
            proof['tools'] = [t.name for t in (await client.list_tools()).tools]
            assert len(proof['tools']) == 6
            async def call(name, args):
                result = await client.call_tool(name,args)
                assert not result.is_error, result.structured_content.get('error')
                return result.structured_content
            projects = await call('observability_projects',{})
            project = next(p['project_id'] for p in projects['projects'] if p['issue_count'])
            listing = await call('observability_issues',{'project_id':project, 'limit':5})
            issue = listing['issues'][0]
            detail = await call('observability_inspect_issue',{'project_id':project,'issue_id':issue['issue_id']})
            assert detail['issue']['issue_id'] == issue['issue_id']
            assert detail['acceptance_status'] == 'not_assessed'
            assert detail['evidence']['raw_logs_available'] is False
            dashboard = await call('observability_dashboard',{'project_id':project})
            assert dashboard['projects']
            rec = await call('observability_recommendation',{'project_id':'context-ledger'})
            assert all(r['acceptance_status']=='not_assessed' for r in rec['recommendations'])
            ui = await client.read_resource('ui://developer-observability/dashboard')
            assert ui.contents[0].mime_type == 'text/html;profile=mcp-app'
            proof.update(project_id=project,issue_count=listing['total'],evidence_refs=len(detail['evidence']['event_refs']),
                         resource_bytes=len(ui.contents[0].text.encode()),coverage=projects['coverage'],freshness=projects['freshness'],
                         detail_bytes=len(json.dumps(detail).encode()),read_wall_ms=round((time.monotonic()-started)*1000))
            if decision:
                listing = await call('observability_issues',{'project_id':project,'limit':1})
                answer = await call('observability_decide',{'project_id':project,'snapshot_id':listing['snapshot_id'],
                    'state':'Acceptance probe only. No source change is proposed.\nA prior issue has incomplete root-cause evidence.',
                    'question':{'type':'choice','instructions':'Choose the warranted next step.',
                                'criteria':{'inspect':'Inspect evidence before choosing a fix','claim_fixed':'Declare a verified fix without testing'}}})
                proof['decision_tested'] = True
                proof['decision'] = answer['decision']
                proof['decision_ms'] = answer['ms']
                proof['decision_usage'] = answer['usage']
    after = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked if p.exists()}
    assert before == after, 'read/decision unexpectedly mutated recommendation state'
    proof['recommendation_state_unchanged'] = True
    proof['host_rendering_verified'] = False
    output.write_text(json.dumps(proof,indent=2)+'\n')
    print(json.dumps(proof,indent=2))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--launcher',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--decision',action='store_true')
    args=parser.parse_args()
    asyncio.run(probe(args.launcher,args.output,args.decision))


if __name__=='__main__':
    main()
