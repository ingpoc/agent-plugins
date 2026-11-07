"""Read-only probe of the installed Developer Observability MCP."""
import asyncio
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def probe(package: Path) -> None:
    async with stdio_client(StdioServerParameters(command=str(package/'bin/developer-observability'))) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            if 'observability_diagnose' not in names:
                raise RuntimeError('installed diagnosis tool missing')
            projects = await client.call_tool('observability_projects', {})
            if projects.is_error:
                raise RuntimeError('installed projects call failed')
            project = next((p['project_id'] for p in projects.structured_content['projects'] if p['issue_count']), None)
            if not project:
                raise RuntimeError('no findings available for scoped installed verification')
            brief = await client.call_tool('observability_diagnose', {'project_id': project})
            if brief.is_error:
                raise RuntimeError('installed diagnosis failed')
            data = brief.structured_content
            rows = [row for key in ('top_failures', 'slowest', 'redundancy', 'layout', 'storage')
                    for row in data['brief'].get(key, [])]
            if not rows:
                raise RuntimeError('diagnosis returned no issue to inspect')
            args = {'project_id': project, 'issue_ids': [rows[0]['issue_id']], 'snapshot_id': data['snapshot_id']}
            detail = await client.call_tool('observability_diagnose', args)
            if detail.is_error or detail.structured_content['count'] != 1:
                raise RuntimeError('installed diagnosis expansion failed')
            stale = await client.call_tool('observability_diagnose', {**args, 'snapshot_id': 'old'})
            if not stale.is_error or stale.structured_content.get('error') != 'snapshot_changed':
                raise RuntimeError('installed snapshot guard failed')
            resource = await client.read_resource('ui://developer-observability/dashboard')
            if resource.contents[0].mime_type != 'text/html;profile=mcp-app':
                raise RuntimeError('installed dashboard resource failed')
            print(f"Installed MCP probe passed; snapshot freshness: {data['freshness']}")


if __name__ == '__main__':
    asyncio.run(probe(Path(sys.argv[1])))
