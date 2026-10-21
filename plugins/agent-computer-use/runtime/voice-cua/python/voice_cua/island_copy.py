"""Island copy contract — compact titles and human error copy.

Compact ``title``: at most 28 characters, verb + target ("Opening netflix.com",
"Reading results"). Anything longer moves to ``detail`` (expanded-only); the
title is cut on a word boundary, never with an ellipsis, so the target stays
whole whenever verb + target fit. Errors are mapped to short human copy; the
raw text is logged only (never shown). Mirrors CUAService ``IslandCopy``.
"""

from __future__ import annotations

import re

MAX_TITLE = 28
# Error copy comes from the fixed table below (pre-vetted, ≤ 32 incl. target).
MAX_ERROR = 32

# Harmless Realtime truncate race — never shown on the island.
BENIGN_ERROR_PATTERNS = (
    re.compile(r"already shorter than", re.I),
    re.compile(r"cancel_not_active|no active response", re.I),
    re.compile(r"conversation_item_not_found|item_not_found", re.I),
    re.compile(r"buffer_clear_empty", re.I),
)

# (pattern, copy) — first match wins; "{target}" is the app / control if known.
ERROR_COPY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmic\b|microphone|sounddevice|input device", re.I), "Can't hear you, check mic"),
    (re.compile(r"websocket|connection (reset|closed|lost|refused)|reconnect|ping timeout|session (expired|not found)", re.I),
     "Connection dropped, reconnecting"),
    (re.compile(r"label not found|control not found|no such element|click", re.I), "Couldn't click {target}"),
    (re.compile(r"app not found|can't open app|unable to open|not installed", re.I), "Couldn't open {target}"),
    (re.compile(r"no window", re.I), "No {target} window"),
    (re.compile(r"user denied|denied", re.I), "Stopped — you said no"),
    (re.compile(r"comet.*(unavailable|busy)|extension", re.I), "Comet isn't available"),
    (re.compile(r"verif|unverified|expect", re.I), "Missed that, retrying"),
    (re.compile(r"timeout|timed out", re.I), "That took too long"),
    (re.compile(r"config|invalid_request|invalid_value|realtime", re.I), "Voice hiccup, still here"),
)
FALLBACK_ERROR = "Missed that, retrying"

HUMAN_COPY = frozenset(
    {FALLBACK_ERROR, "Can't hear you, check mic", "Connection dropped, reconnecting", "Stopped — you said no", "Comet isn't available",
     "That took too long", "Voice hiccup, still here"}
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def compact(title: str, detail: str = "") -> tuple[str, str]:
    """Clamp ``title`` to MAX_TITLE; overflow moves to the front of ``detail``."""
    title, detail = _squash(title), _squash(detail)
    if len(title) <= MAX_TITLE:
        return title, detail
    if " · " in title:
        head, rest = title.split(" · ", 1)
        if len(head) <= MAX_TITLE:
            return head, _squash(f"{rest} · {detail}" if detail else rest)
    words = title.split(" ")
    kept: list[str] = []
    for word in words:
        if len(" ".join([*kept, word])) > MAX_TITLE:
            break
        kept.append(word)
    if not kept:  # one giant token (URL, path): keep its start, no ellipsis
        head, rest = title[:MAX_TITLE], title[MAX_TITLE:]
    else:
        head, rest = " ".join(kept), " ".join(words[len(kept):])
    return head, _squash(f"{rest} · {detail}" if detail else rest)


def is_benign_error(raw: str) -> bool:
    return any(p.search(raw or "") for p in BENIGN_ERROR_PATTERNS)


def human_error(raw: str, *, target: str = "") -> str | None:
    """Human copy for an error, or None when it must not be shown at all."""
    raw = _squash(raw)
    if is_benign_error(raw):
        return None
    if raw in HUMAN_COPY or (raw.startswith(("Couldn't ", "No ")) and len(raw) <= MAX_ERROR):
        return raw
    name = _squash(target)[:16] or "that"
    for pattern, copy in ERROR_COPY:
        if pattern.search(raw):
            return copy.format(target=name)[:MAX_ERROR].rstrip()
    return FALLBACK_ERROR
