"""Real stdio MCP handshake, schemas, errors and app resource; isolated backend."""
import asyncio
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

    async def exercise(self, modern=True):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'telemetry').mkdir()
            (base/'telemetry/__init__.py').write_text('')
            (base/'telemetry/observability_api.py').write_text('''class ObservabilityAPI:
    def __init__(self,*args): pass
    def projects(self): return {"projects":[],"snapshot_id":"fixture","freshness":"fresh"}
    def issues(self,*args): raise ValueError("unknown_project: private detail")
''')
            config = base/'config.json'
            config.write_text(json.dumps({'backend_root':str(base),'runtime':str(base), 'projects':{},'decider_script':str(base/'unused')}))
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'runtime/server.py')],
                                           env={**os.environ, 'OBSERVABILITY_CONFIG':str(config)})
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
                    self.assertEqual(len(tools), 6)
                    dashboard = next(t for t in tools if t.name == 'observability_dashboard')
                    self.assertEqual(dashboard.meta['ui']['resourceUri'], 'ui://developer-observability/dashboard')
                    answer = await client.call_tool('observability_projects', {})
                    self.assertEqual(answer.structured_content['snapshot_id'], 'fixture')
                    failure = await client.call_tool('observability_issues', {'project_id':'other'})
                    self.assertTrue(failure.is_error)
                    self.assertNotIn('private detail', json.dumps(failure.model_dump()))
                    resource = await client.read_resource('ui://developer-observability/dashboard')
                    html = resource.contents[0]
                    self.assertEqual(html.mime_type, 'text/html;profile=mcp-app')
                    self.assertIn('See what needs attention', html.text)
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
