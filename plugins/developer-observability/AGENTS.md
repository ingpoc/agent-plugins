# Developer Observability — installation

Install this portable package through the client's marketplace. The collection
root is not a plugin. The package is self-contained: the read-side backend is
vendored in `runtime/backend/` and reads a prepared `findings.json`. A small
stdlib collector (`runtime/collector/`, `observability_collect`,
`bin/developer-observability collect`) writes its own
`~/.local/share/developer-observability/collected/findings.json`, merged on read;
it never writes another producer's file.

1. Run `python3 runtime/install.py` (Python 3.11+) from the installed plugin
   folder. It installs pinned requirements in
   `~/.local/share/developer-observability/runtime` without changing global
   Python, and writes a default `~/.config/developer-observability/config.json`
   only if none exists. It never overwrites a config. `OBSERVABILITY_PYTHON`
   may select another interpreter with the same dependencies; the launcher
   fails loud when the runtime is absent.
2. Optional binding: `python3 runtime/configure.py --runtime DIR [--decider PATH]
   [--project id,label=Name,labels=a|b,owners=owner:x] [--no-discover]` before
   the first install, or edit the config by hand. `OBSERVABILITY_CONFIG` selects
   another config. No secrets belong in it. Known producer paths (the Kindle
   snapshot runtime, the Mac Decider skill) are detected when present.
3. Project discovery names projects from each finding's reported project label,
   else its `owner:` ref; configured IDs win. Unassigned findings are counted,
   not guessed.
4. Manifests: root `mcp.json` (sh resolver over `${PLUGIN_ROOT}`,
   `${CURSOR_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`, failing with
   `PLUGIN_ROOT_UNRESOLVED`), `.cursor-plugin/`, `.claude-plugin/`, and Grok's
   `.mcp.json`. No manifest sets `cwd`. Verify seven tools, a real
   `observability_projects`, a scoped issue and the `ui://` resource through the
   installed client. Without any findings the projects call returns
   `data_source.status = no_findings` and a hint; that is not a failure of the
   plugin, and it is not data. Never mark data available from health alone.
5. The dashboard uses MCP Apps (`io.modelcontextprotocol/ui`,
   `text/html;profile=mcp-app`). Hosts without Apps still receive structured
   tools. A plugin listing does not prove host rendering; test it separately.

Build UI only after edits: `npm ci --ignore-scripts && npm run build`.
The prebuilt self-contained `ui/dashboard.html` ships; runtime needs no Node or
CDN. Tests: `python3 -m unittest discover -s tests` with the plugin runtime;
`tests/test_drift.py` compares the vendored backend with the upstream producer
checkout (`OBSERVABILITY_UPSTREAM_ROOT` or the legacy config `backend_root`, test-only and never read at runtime) and skips
when it is absent. Re-vendor when it fails: copy the four upstream files,
reapply the marked edits, update `runtime/backend/VENDORED.json`. Package
validation is owned by collection `scripts/check.py`. `tests/serve_host.py` is
an optional loopback acceptance host, not a production service.

No credential collection, repair execution, collector restart or arbitrary log
reader is exposed. Raw evidence is a bounded redacted projection, never full
source transcripts. No MCP tool authenticates repair acceptance.
