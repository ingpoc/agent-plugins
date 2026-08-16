"""Comet Control extension bridge for browser control."""

from __future__ import annotations

from pathlib import Path

from .tools import (
    COMET_CONTROL_BROWSER_SCHEMA,
    _check_comet_control_available,
    _handle_comet_control_browser,
)


def _canonical_skill_path() -> Path:
    """Return the single repository-owned Comet Control skill entrypoint."""
    skill_path = (
        Path(__file__).resolve().parents[2] / "skills" / "comet-control" / "SKILL.md"
    ).resolve()
    if not skill_path.is_file():
        raise FileNotFoundError(f"Comet Control skill is missing: {skill_path}")
    return skill_path


def register(ctx) -> None:
    """Register the Comet Control bridge tool and explicit skill."""
    ctx.register_tool(
        name="comet_control_browser",
        toolset="comet_control",
        schema=COMET_CONTROL_BROWSER_SCHEMA,
        handler=_handle_comet_control_browser,
        check_fn=_check_comet_control_available,
        emoji="🌐",
    )
    ctx.register_skill(
        "comet-control",
        _canonical_skill_path(),
        "Control Comet through the Comet Control extension bridge.",
    )
