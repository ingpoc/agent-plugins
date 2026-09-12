#!/usr/bin/env python3
"""Render the small, action-local part of two AX markdown snapshots."""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import re


_NODE = re.compile(r"^(?P<indent>\s*)\[(?P<index>\d+)\]\s+(?P<body>\S.*?)(?:\s+\{[-\d.]+,[-\d.]+\s+[-\d.]+x[-\d.]+\})?$", re.DOTALL)
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
    records: list[str] = []
    for line in lines:
        if _NODE.fullmatch(line):
            # A node-shaped continuation inside an open value is ambiguous.
            if records and not _valid_record(records[-1]):
                return None
            records.append(line)
        elif records and ' value="' in records[-1]:
            # Unescaped AX text can imitate a complete node after a quote.
            # If both sides of that boundary have values, do not guess IDs.
            if len(records) > 1 and ' value="' in records[-2]:
                return None
            records[-1] += "\n" + line
        else:
            return None
    nodes: list[tuple[str, str]] = []
    for record in records:
        match = _NODE.fullmatch(record)
        if not match or not _valid_record(record):
            return None
        semantic = match.group("indent") + match.group("body")
        nodes.append((record, _GEOMETRY.sub("", semantic)))
    return window, nodes


def _valid_record(record: str) -> bool:
    body = _GEOMETRY.sub("", record)
    if ' value="' in body:
        prefix, _, value = body.partition(' value="')
        return (prefix.count('"') % 2 == 0
                and (value.endswith('"') or value.endswith('" [pressable]')))
    return "\n" not in body and body.count('"') % 2 == 0


def _text_value(semantic: str) -> tuple[str, str, str] | None:
    prefix, separator, remainder = semantic.partition(' value="')
    if not separator or not re.fullmatch(r'\s*AX(?:TextArea|TextField|SearchField)(?: "[^"\n]*")?', prefix):
        return None
    suffix = ' [pressable]' if remainder.endswith('" [pressable]') else ''
    return prefix, remainder[:-(len(suffix) + 1)], suffix


def _text_change(old: str, current: tuple[str, str]) -> str | None:
    previous = _text_value(old)
    updated = _text_value(current[1])
    if not previous or not updated or previous[::2] != updated[::2]:
        return None
    old_value, new_value = previous[1], updated[1]
    if max(len(old_value), len(new_value)) <= 240 and "\n" not in old_value + new_value:
        return None
    start = 0
    limit = min(len(old_value), len(new_value))
    while start < limit and old_value[start] == new_value[start]:
        start += 1
    suffix = 0
    while suffix < limit - start and old_value[-suffix - 1] == new_value[-suffix - 1]:
        suffix += 1
    old_end, new_end = len(old_value) - suffix, len(new_value) - suffix
    excerpt_start = max(0, start - 32)
    excerpt_end = min(len(new_value), max(start + 32, new_end), excerpt_start + 160)
    excerpt = json.dumps(new_value[excerpt_start:excerpt_end], ensure_ascii=False)
    node = _NODE.fullmatch(current[0])
    return (f"~ [{node.group('index')}] {updated[0].strip()}: text changed; "
            f"character ranges (0-based, end-exclusive) old[{start}:{old_end}] -> new[{start}:{new_end}]; "
            f"length {len(old_value)} -> {len(new_value)}; "
            f"current excerpt[{excerpt_start}:{excerpt_end}]={excerpt} (excerpt, not full value)")


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
        if tag == "replace" and old_end - old_start == new_end - new_start == 1:
            summary = _text_change(before_semantics[old_start], after_nodes[new_start])
            if summary:
                lines.append(summary)
                continue
        if tag in {"delete", "replace"}:
            lines.extend(f"- {before_semantics[index]}" for index in range(old_start, old_end))
        if tag in {"insert", "replace"}:
            marker = "+" if tag == "insert" else "~"
            lines.extend(f"{marker} {after_nodes[index][0]}" for index in range(new_start, new_end))
    if not changed:
        unchanged = "No changes during action. IDs omitted here are stale; use labels or state for other targets."
        return unchanged if len(unchanged) <= len(after) else after
    delta = "\n".join(lines)
    return after if len(delta) > len(after) else delta
