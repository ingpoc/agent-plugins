"""Postcondition verification and screenshot policy for compact computer use."""
from __future__ import annotations

import os
import re
from typing import Any

from compact_form_fills import expectation_supports_value, expectation_value_field_matches

_BIDI = dict.fromkeys(
    map(
        ord,
        "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
        "\u2066\u2067\u2068\u2069\ufeff",
    )
)


_DASH = str.maketrans({
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-",
    0x2014: "-", 0x2015: "-", 0x2212: "-", 0xFE63: "-", 0xFF0D: "-",
})


def _norm_text(s: str) -> str:
    return (s or "").translate(_BIDI).translate(_DASH).strip().lower()


def expect_verified(expect: str, tree_text: str) -> bool:
    """Match expect against AXStaticText, AXTextArea, AXTextField, AXCell values — never button titles."""
    needle = _norm_text(expect)
    if not needle:
        return True
    values = [
        _norm_text(v)
        for v in re.findall(
            r'AX(?:StaticText|TextArea|TextField|Cell)[^\n]*value="([^"]*)"',
            tree_text,
        )
    ]
    values.extend(
        _norm_text(v) for v in re.findall(r'AXCell "([^"]*)"', tree_text)
    )
    for v in values:
        if not v:
            continue
        if needle == v:
            return True
        if len(needle) >= 2 and needle in v:
            return True
    return False


def expect_is_new(expect: str, before_tree: str, after_tree: str) -> bool:
    """True when expect is a new value. Empty expect skips the check."""
    if not _norm_text(expect):
        return True
    return expect_verified(expect, after_tree) and not expect_verified(
        expect, before_tree
    )


def expectation_is_supported(expect: Any) -> bool:
    if isinstance(expect, str):
        return bool(_norm_text(expect))
    if isinstance(expect, list):
        return bool(expect) and all(expectation_is_supported(item) for item in expect)
    if not isinstance(expect, dict) or not expect:
        return False
    if expectation_supports_value(expect):
        return True
    return any(
        bool(_norm_text(str(expect.get(key) or "")))
        for key in ("text", "not_text", "path")
    )


def expectation_is_new(
    expect: Any,
    before_tree: str,
    after_tree: str,
    results: list[dict[str, Any]] | None = None,
    after_state: Any = None,
) -> bool:
    """Verify compact positive/negative postconditions against settled AX values."""
    if isinstance(expect, str):
        return expect_is_new(expect, before_tree, after_tree)
    if isinstance(expect, list):
        return bool(expect) and all(
            expectation_is_new(item, before_tree, after_tree, results, after_state)
            for item in expect
        )
    if not isinstance(expect, dict):
        return False
    checks: list[bool] = []
    if expect.get("text"):
        checks.append(expect_is_new(str(expect["text"]), before_tree, after_tree))
    if expect.get("not_text"):
        needle = str(expect["not_text"])
        checks.append(
            expect_verified(needle, before_tree)
            and not expect_verified(needle, after_tree)
        )
    if expect.get("path"):
        wanted = os.path.normpath(str(expect["path"]))
        checks.append(any(
            os.path.normpath(str(item.get("path"))) == wanted
            for item in (results or [])
            if item.get("path")
        ))
    if expectation_supports_value(expect):
        checks.append(expectation_value_field_matches(
            expect, after_state if isinstance(after_state, dict) else {}
        ))
    return bool(checks) and all(checks)


def _ax_has_text_values(tree_text: str) -> bool:
    return bool(re.search(
        r'AX(?:StaticText|TextArea|TextField|Cell)[^\n]*value="[^"]+"|AXCell "[^"]+"',
        tree_text or "",
    ))


def _window_is_thumb(state: Any) -> bool:
    try:
        area = float(state.get("windowWidth") or 0) * float(state.get("windowHeight") or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    return 0 < area < 80 * 80


def screenshot_needed(payload: dict[str, Any], before: Any, after: Any, arguments: dict[str, Any]) -> bool:
    if arguments.get("screenshot") is True or _window_is_thumb(before) or _window_is_thumb(after):
        return True
    if payload.get("ok") and payload.get("verified"):
        return False
    after_text = str(after.get("text") or "") if isinstance(after, dict) else ""
    before_text = str(before.get("text") or "") if isinstance(before, dict) else ""
    return not after_text.strip() or (
        not _ax_has_text_values(after_text) and not _ax_has_text_values(before_text)
    )


def apply_screenshot_policy(payload, *, before, after, arguments, fetch=None):
    if not screenshot_needed(payload, before, after, arguments):
        payload.update(screenshot_before=None, screenshot_after=None, screenshot=None)
        return payload
    before_shot = before.get("screenshot") if isinstance(before, dict) else None
    after_shot = after.get("screenshot") if isinstance(after, dict) else None
    if after_shot is None and before_shot is None and fetch is not None:
        extra = fetch()
        after_shot = extra.get("screenshot") if isinstance(extra, dict) else None
    payload.update(screenshot_before=before_shot, screenshot_after=after_shot, screenshot=after_shot or before_shot)
    return payload



