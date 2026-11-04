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
                                  'latest_evidence': {'first_error_line': 'private detail'}}]}))
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
                    self.assertEqual(len(tools), 8)
                    self.assertIn('observability_storage', [t.name for t in tools])
                    self.assertIn("observability_collect", [t.name for t in tools])
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
                    issues = await client.call_tool('observability_issues', {'project_id': 'a'})
                    self.assertEqual(issues.structured_content['total'], 1)
                    failure = await client.call_tool('observability_issues', {'project_id':'other'})
                    self.assertTrue(failure.is_error)
                    dumped = json.dumps(failure.model_dump())
                    self.assertNotIn('private detail', dumped)
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
