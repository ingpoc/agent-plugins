#!/usr/bin/env python3
"""Build ui/ledger-v2.html: one self-contained file (inline CSS, JS, subset Geist woff2).
python3 ui-src/build.py [--check]   --check fails if the committed bundle differs from source."""
import base64, pathlib, sys
SRC = pathlib.Path(__file__).resolve().parent
OUT = SRC.parent / "ui" / "ledger-v2.html"
b64 = lambda n: base64.b64encode((SRC / n).read_bytes()).decode()
css = (SRC / "style.css").read_text().replace("__SANS__", b64("sans.woff2")).replace("__MONO__", b64("mono.woff2"))
html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'self'">
<meta name="color-scheme" content="light dark">
<title>Context Ledger</title>
<style>{css}</style>
</head>
<body>
{(SRC / "body.html").read_text()}
<script>
{(SRC / "app.js").read_text()}
</script>
</body>
</html>
'''
if "--check" in sys.argv:
    if not OUT.is_file() or OUT.read_text() != html:
        sys.exit(f"FAIL {OUT.name} is stale; fix: python3 ui-src/build.py")
    print(f"ok {OUT.name} matches ui-src")
else:
    OUT.write_text(html)
    print(OUT, len(html.encode()))
