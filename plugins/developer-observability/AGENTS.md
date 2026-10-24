# Developer Observability — installation

Install this portable package through the client's marketplace. The collection
root is not a plugin. Owner backend: the existing Kindle project's
`telemetry/observability_api.py`; this package is a thin facade, not a collector.

1. Run `python3 runtime/install.py` with Python 3.12+. This installs pinned
   requirements in `~/.local/share/developer-observability/runtime`, without
   changing global Python. `OBSERVABILITY_PYTHON` may select another interpreter
   with those exact dependencies. The launcher fails if its runtime is absent.
2. Bind the existing backend once:
   `python3 runtime/configure.py --backend-root /absolute/path/to/kindle-project`.
   It validates the adapter and writes a private, metadata-only
   `~/.config/developer-observability/config.json`; it refuses to overwrite.
   An existing config can be explicitly edited by its owner. Optional
   `OBSERVABILITY_CONFIG` selects a separate binding; no secrets belong in it.
3. Configuration owns project IDs and owner/label matches. Review provisional
   labels before using them as project attribution. Unassigned findings are
   counted, not guessed. The initial bindings cover Samantha, Kindle and the
   Context Ledger recommendation pilot. Add other projects explicitly.
4. Load `mcp.json`. Cursor uses `.cursor-plugin/mcp.json` to expand its plugin root.
   Verify six tools, real project retrieval, a scoped issue, and the `ui://`
   resource through the installed MCP client. Run an explicit nonsensitive
   decision only when authorized. Never mark data available from health alone.
5. The dashboard uses MCP Apps (`io.modelcontextprotocol/ui`,
   `text/html;profile=mcp-app`). Hosts without Apps still receive structured
   tools. A plugin listing does not prove host rendering; test it separately.

Build UI only after edits: `npm ci --ignore-scripts && npm run build`.
The prebuilt self-contained `ui/dashboard.html` ships; runtime needs no Node or
CDN. Tests: `python3 -m unittest discover -s tests`; package validation is owned
by collection `scripts/check.py`. `tests/serve_host.py` is an optional loopback
acceptance host, not a production service. Stop it after testing.

No credential collection, repair execution, collector restart or arbitrary log
reader is exposed. Raw evidence is a bounded redacted projection, never full
source transcripts. No MCP tool authenticates repair acceptance.
