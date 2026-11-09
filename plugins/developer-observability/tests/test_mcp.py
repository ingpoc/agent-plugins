"""Real stdio MCP handshake, schemas, errors and app resource over an isolated findings.json."""
import asyncio
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


class MCPTests(unittest.TestCase):
    def test_stdio_and_app_contract(self):
        asyncio.run(self.exercise())

    def test_legacy_text_client(self):
        asyncio.run(self.exercise(modern=False))

    def test_empty_state_over_stdio(self):
        asyncio.run(self.exercise(findings=False))

    async def exercise(self, modern=True, findings=True):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            if findings:
                (base/'findings.json').write_text(json.dumps({
                    'generated_at': datetime.now(timezone.utc).isoformat(),
                    'findings': [{'finding_key': 'k1', 'owner_ref': 'owner:a', 'headline': 'h', 'status': 'active',
                                  'latest_evidence': {'first_error_line': 'token=sk-abcdefghijklmnopqrstuvwxyz'}}]}))
                (base/'collected').mkdir()
                (base/'collected'/'findings.json').write_text(json.dumps({
                    'generated_at': datetime.now(timezone.utc).isoformat(), 'findings': [],
                    'version_scopes': [{'project': 'a', 'version': '2.0', 'attribution': 'event',
                                        'findings': [{'finding_key': 'col-scoped', 'owner_ref': 'owner:a',
                                                      'headline': 'versioned', 'status': 'active'}],
                                        'session_timelines': []}]}))
            config = base/'config.json'
            # Collector output stays in the temp dir: never merge the machine's real collected findings.
            config.write_text(json.dumps({'runtime': str(base), 'decider_script': None,
                                          'collector': {'out': str(base/'collected')},
                                          'retention': {'cache_dirs': [str(base/'cache')], 'trash_dir': str(base/'trash')},
                                          'projects': {'a': {'owners': ['owner:a']}}}))
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'runtime/server.py')],
                                           env={**os.environ, 'OBSERVABILITY_CONFIG':str(config), 'OBSERVABILITY_HOME': str(base)})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, extensions={'io.modelcontextprotocol/ui': {'mimeTypes':['text/html;profile=mcp-app']}}) as client:
                    if modern:
                        result = await client.discover()
                        self.assertEqual(client.protocol_version, '2026-07-28')
                        self.assertIn('io.modelcontextprotocol/ui', result.capabilities.extensions)
                    else:
                        await client.initialize()
                        self.assertEqual(client.protocol_version, '2025-11-25')
                    tools = (await client.list_tools()).tools
                    self.assertEqual(len(tools), 12)
                    usage = await client.call_tool('observability_task_usage', {})
                    self.assertFalse(usage.is_error)
                    self.assertEqual(usage.structured_content['status'], 'not_captured')
                    self.assertIn('observability_sessions', [t.name for t in tools])
                    self.assertIn('observability_storage', [t.name for t in tools])
                    self.assertIn("observability_collect", [t.name for t in tools])
                    for name in ('observability_sessions', 'observability_inspect_issue'):
                        schema = next(t for t in tools if t.name == name).model_dump(by_alias=True)['inputSchema']
                        self.assertIn('include_diagnostics', schema['properties'])
                    dashboard = next(t for t in tools if t.name == 'observability_dashboard')
                    self.assertEqual(dashboard.meta['ui']['resourceUri'], 'ui://developer-observability/dashboard')
                    answer = await client.call_tool('observability_projects', {})
                    self.assertFalse(answer.is_error)
                    projects = answer.structured_content
                    if not findings:
                        self.assertEqual(projects['data_source']['status'], 'no_findings')
                        self.assertEqual(projects['projects'][0]['issue_count'], 0)
                        return
                    self.assertEqual(projects['data_source']['status'], 'findings_present')
                    self.assertEqual([(p['project_id'], p['issue_count']) for p in projects['projects']], [('a', 1)])
                    versions = await client.call_tool('observability_version_scopes', {'project_id': 'a'})
                    self.assertEqual(versions.structured_content['version_scopes'], [{'version': '2.0', 'attribution': 'event'}])
                    scoped = await client.call_tool('observability_diagnose', {'project_id': 'a', 'version': '2.0', 'attribution': 'event'})
                    self.assertFalse(scoped.is_error)
                    self.assertEqual(scoped.structured_content['brief']['counts']['total'], 1)
                    self.assertFalse(scoped.structured_content['version_scope']['deployment_verified'])
                    brief = await client.call_tool('observability_diagnose', {'project_id': 'a'})
                    self.assertFalse(brief.is_error)
                    self.assertEqual(brief.structured_content['brief']['counts']['total'], 1)
                    batch = await client.call_tool('observability_diagnose', {'project_id': 'a', 'issue_ids': ['k1', 'missing'], 'snapshot_id': brief.structured_content['snapshot_id']})
                    self.assertFalse(batch.is_error)
                    self.assertEqual(batch.structured_content['count'], 1)
                    self.assertEqual(batch.structured_content['errors'][0]['error'], 'issue_not_in_project')
                    self.assertNotIn('sk-abcdefghijklmnopqrstuvwxyz', json.dumps(batch.model_dump()))
                    stale = await client.call_tool('observability_diagnose', {'project_id': 'a', 'issue_ids': ['k1'], 'snapshot_id': 'old'})
                    self.assertTrue(stale.is_error)
                    self.assertEqual(stale.structured_content['error'], 'snapshot_changed')
                    self.assertIn('fresh diagnosis', stale.structured_content['next_step'])
                    issues = await client.call_tool('observability_issues', {'project_id': 'a'})
                    self.assertEqual(issues.structured_content['total'], 1)
                    failure = await client.call_tool('observability_issues', {'project_id':'other'})
                    self.assertTrue(failure.is_error)
                    dumped = json.dumps(failure.model_dump())
                    self.assertNotIn('sk-abcdefghijklmnopqrstuvwxyz', dumped)
                    self.assertNotIn(str(base), dumped)
                    resource = await client.read_resource('ui://developer-observability/dashboard')
                    html = resource.contents[0]
                    self.assertEqual(html.mime_type, 'text/html;profile=mcp-app')
                    self.assertIn('See what needs attention', html.text)
                    self.assertIn('Occurrence timeline', html.text)
                    self.assertNotIn('cdn.', html.text)
                    self.assertIn('connectDomains', json.dumps(html.meta))

    def test_ui_project_switch_race(self):
        subprocess.run(['node',str(ROOT/'tests/ui_race.mjs')],check=True,capture_output=True)

    def test_shipped_html_script_parses(self):
        html = (ROOT/'ui/dashboard.html').read_text()
        scripts = re.findall(r'<script type="module">(.*?)</script>',html,re.S)
        self.assertEqual(len(scripts),1)
        subprocess.run(['node','--input-type=module','--check'],input=scripts[0],text=True,check=True,capture_output=True)

    def test_bundle_reproducible(self):
        if not (ROOT/'node_modules').exists():
            self.skipTest('UI build dependencies not installed; shipped bundle tested by stdio')
        before = (ROOT/'ui/dashboard.html').read_bytes()
        subprocess.run(['npm','run','build'],cwd=ROOT,check=True,capture_output=True)
        self.assertEqual(before, (ROOT/'ui/dashboard.html').read_bytes())
