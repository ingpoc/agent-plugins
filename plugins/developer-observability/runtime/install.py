"""One install step: pinned MCP runtime in its own venv, then a default config only if none exists."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import configure  # noqa: E402


def runtime_dir() -> Path:
    return Path(os.environ.get('OBSERVABILITY_RUNTIME_DIR') or Path.home() / '.local/share/developer-observability/runtime')


def install_deps(runtime: Path) -> None:
    python = runtime / 'bin/python3'
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(runtime)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r',
                    str(ROOT / 'requirements.txt')], check=True)
    subprocess.run([str(python), '-c', "import mcp.server.mcpserver; import importlib.metadata as m; "
                    "assert m.version('mcp') == '2.2.0'"], check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('OBSERVABILITY_CONFIG') or configure.DEFAULT_CONFIG))
    parser.add_argument('--skip-deps', action='store_true', help='only write the default config (tests)')
    args = parser.parse_args(argv)
    if not args.skip_deps:
        install_deps(runtime_dir())
    config = configure.default_config()
    wrote = configure.write_config(args.config, config)
    print(('Wrote default config ' if wrote else 'Kept existing config ') + configure._tilde(args.config))
    if wrote and config['runtime'] == configure.DEFAULT_DATA:
        Path(configure.DEFAULT_DATA).expanduser().mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.skip_deps:
        print('Pinned local MCP runtime ready.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
