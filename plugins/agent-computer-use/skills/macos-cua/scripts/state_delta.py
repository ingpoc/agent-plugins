#!/usr/bin/env python3
"""Render the small, action-local part of two AX markdown snapshots."""

from __future__ import annotations

from difflib import SequenceMatcher
import re


_NODE = re.compile(r"^(?P<indent>\s*)\[(?P<index>\d+)\]\s+(?P<body>\S.*?)(?:\s+\{[-\d.]+,[-\d.]+\s+[-\d.]+x[-\d.]+\})?$")
_WINDOW = re.compile(r'^Window:\s+"(?P<title>.*)"$')
_GEOMETRY = re.compile(r"\s+\{[-\d.]+,[-\d.]+\s+[-\d.]+x[-\d.]+\}$")


def _parse(text: str) -> tuple[str | None, list[tuple[str, str]]] | None:
    if not text.strip():
        return None
    lines = text.splitlines()
    window = None
    if lines and (match := _WINDOW.fullmatch(lines[0])):
        window = match.group("title")
        lines = lines[1:]
    if not lines:
        return None
    nodes: list[tuple[str, str]] = []
    for line in lines:
        match = _NODE.fullmatch(line)
        if not match or line.count('"') % 2:
            return None
        semantic = match.group("indent") + match.group("body")
        nodes.append((line, _GEOMETRY.sub("", semantic)))
    return window, nodes


def action_state_text(before: str, after: str) -> str:
    """Return an AX delta, falling back to the complete current snapshot."""
    parsed_before = _parse(before)
    parsed_after = _parse(after)
    if not parsed_before or not parsed_after:
        return after

    before_window, before_nodes = parsed_before
    after_window, after_nodes = parsed_after
    if before_window != after_window:
        return after
    if before_window is None and (before_nodes[0][1].startswith("AXWindow") or after_nodes[0][1].startswith("AXWindow")) and before_nodes[0][1] != after_nodes[0][1]:
        return after

    before_semantics = [semantic for _, semantic in before_nodes]
    after_semantics = [semantic for _, semantic in after_nodes]
    matcher = SequenceMatcher(None, before_semantics, after_semantics, autojunk=False)
    lines = [
        "Changes during action. IDs shown are current; omitted IDs are stale, use labels or state for other targets."
    ]
    changed = False
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed = True
        if tag in {"delete", "replace"}:
            lines.extend(f"- {before_semantics[index]}" for index in range(old_start, old_end))
        if tag in {"insert", "replace"}:
            marker = "+" if tag == "insert" else "~"
            lines.extend(f"{marker} {after_nodes[index][0]}" for index in range(new_start, new_end))
    if not changed:
        return "No changes during action. IDs omitted here are stale; use labels or state for other targets."
    delta = "\n".join(lines)
    return after if len(delta) > len(after) else delta
