"""Clipboard-based secret inject — never type_text secrets."""

from __future__ import annotations

import subprocess
import time
from typing import Any


def clear_clipboard() -> None:
    subprocess.run(["pbcopy"], input=b"", check=False)


def set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def clipboard_paste_secret(app: str, secret: str, backend: Any) -> dict[str, Any]:
    """Put secret on clipboard, Cmd+V into app, caller must clear clipboard."""
    set_clipboard(secret)
    time.sleep(0.05)
    return backend.act(app, {"app": app, "key": "cmd+v"})
