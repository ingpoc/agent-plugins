"""Install pinned MCP dependencies in a dedicated local runtime, never global Python."""
from pathlib import Path
import subprocess
import venv

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home()/'.local/share/developer-observability/runtime'


def main():
    python = RUNTIME/'bin/python3'
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(RUNTIME)
    subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(ROOT/'requirements.txt')],check=True)
    subprocess.run([str(python),'-c',"import mcp.server.mcpserver; import importlib.metadata as m; assert m.version('mcp') == '2.2.0'"],check=True)
    print('Pinned local MCP runtime ready.')


if __name__ == '__main__':
    main()
