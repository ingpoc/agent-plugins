"""Invoke the installed Observability MCP when cloud chat lacks native tools."""
import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', help='Tool name, or list to show schemas')
    parser.add_argument('arguments', nargs='?', default='{}', help='JSON object')
    args = parser.parse_args()
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        parser.error('arguments must be a JSON object')
    root = Path.home() / '.codex/plugins/cache/ingpoc-agent-plugins/developer-observability'
    versions = [p for p in root.iterdir() if (p / 'plugin.json').is_file()]
    package = max(versions, key=lambda p: tuple(int(n) for n in p.name.split('.')))
    async with stdio_client(StdioServerParameters(command=str(package / 'bin/developer-observability'))) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.list_tools() if args.tool == 'list' else await client.call_tool(args.tool, arguments)
            print(result.model_dump_json(by_alias=True, exclude_none=True, indent=2))
            return 1 if getattr(result, 'is_error', False) else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
