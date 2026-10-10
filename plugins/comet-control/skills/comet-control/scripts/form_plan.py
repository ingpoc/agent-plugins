#!/usr/bin/env python3
"""One-pass FILL/CHECK/CLICK/SKIP form plans (bookmark #29, CUA-S1-FORMS pattern).

Plan the whole form once from a supplied document, validate it in code, order it,
then compile it to ONE batched send. Submit is approval-gated and never compiled
unless ``approve_submit=True``.

Routing (Context Ledger): web forms in a Comet lease compile to comet-control
``{"actions": [...]}``; only native macOS forms compile to macos-cua ``act`` steps.
macos-cua never drives Comet page DOM.

The planner here is deterministic label matching. A model planner may replace
``plan_form`` later; ``validate_plan`` stays the gate either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
import argparse
import json
import sys
from typing import Any

FILL, CHECK, CLICK, SKIP = "FILL", "CHECK", "CLICK", "SKIP"
TEXT_KINDS = {"text", "email", "tel", "number", "date", "time", "textarea", "url", "search"}
CHOICE_KINDS = {"checkbox", "radio", "select"}
SECRET_RE = re.compile(r"pass(word|code)|otp|one[- ]time|cvv|cvc|\bpin\b|mfa|2fa|verification code|captcha", re.I)
SUBMIT_RE = re.compile(r"\b(submit|pay|place order|confirm|send|apply|book|purchase|sign up|register|finish)\b", re.I)
_ORDER = {FILL: 0, CHECK: 1, CLICK: 2, SKIP: 3}


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    kind: str  # text|email|tel|number|date|textarea|select|checkbox|radio|button|file|password
    required: bool = False
    options: tuple[str, ...] = ()
    dom_index: int = 0

    @property
    def secret(self) -> bool:
        return self.kind == "password" or bool(SECRET_RE.search(self.label))

    @property
    def submit(self) -> bool:
        return self.kind == "button" and bool(SUBMIT_RE.search(self.label))


@dataclass(frozen=True)
class Step:
    op: str
    field_id: str
    value: Any = None
    reason: str = ""


@dataclass
class Validation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    gated_submit: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _match_key(label: str, document: dict[str, Any], aliases: dict[str, list[str]]) -> list[str]:
    nl = _norm(label)
    hits = []
    for key in document:
        names = [key, *aliases.get(key, [])]
        if any(_norm(n) and (_norm(n) == nl or re.search(rf"\b{re.escape(_norm(n))}\b", nl)) for n in names):
            hits.append(key)
    return hits


def plan_form(
    fields: list[Field],
    document: dict[str, Any],
    *,
    aliases: dict[str, list[str]] | None = None,
    click_labels: tuple[str, ...] = (),
) -> list[Step]:
    """One pass: every field gets exactly one op. Ambiguity and secrets become SKIP."""
    aliases = aliases or {}
    steps: list[Step] = []
    for f in fields:
        if f.kind == "button":
            if f.label in click_labels or f.submit:
                steps.append(Step(CLICK, f.id))
            else:
                steps.append(Step(SKIP, f.id, reason="not_requested"))
            continue
        if f.secret:
            steps.append(Step(SKIP, f.id, reason="secret_human"))
            continue
        if f.kind == "file":
            steps.append(Step(SKIP, f.id, reason="file_upload_separate"))
            continue
        keys = _match_key(f.label, document, aliases)
        if len(keys) != 1:
            steps.append(Step(SKIP, f.id, reason="ambiguous" if keys else "no_source_value"))
            continue
        value = document[keys[0]]
        if f.kind in CHOICE_KINDS:
            if f.kind == "checkbox":
                steps.append(Step(CHECK, f.id, bool(value)))
            else:
                steps.append(Step(CHECK, f.id, str(value)))
        else:
            steps.append(Step(FILL, f.id, str(value)))
    return steps


def validate_plan(
    plan: list[Step], fields: list[Field], document: dict[str, Any], *, approve_submit: bool = False
) -> Validation:
    """Code gate. Fails closed before any dispatch."""
    by_id = {f.id: f for f in fields}
    errs: list[str] = []
    gated: list[str] = []
    seen: dict[str, int] = {}
    doc_values = {str(v) for v in document.values()} | {str(bool(v)) for v in document.values()}
    for s in plan:
        seen[s.field_id] = seen.get(s.field_id, 0) + 1
        f = by_id.get(s.field_id)
        if f is None:
            errs.append(f"unknown_field:{s.field_id}")
            continue
        if s.op not in _ORDER:
            errs.append(f"bad_op:{s.field_id}:{s.op}")
        elif s.op == FILL:
            if f.kind not in TEXT_KINDS:
                errs.append(f"fill_on_{f.kind}:{f.id}")
            if f.secret:
                errs.append(f"secret_in_plan:{f.id}")
            if str(s.value) not in doc_values:
                errs.append(f"invented_value:{f.id}")
        elif s.op == CHECK:
            if f.kind not in CHOICE_KINDS:
                errs.append(f"check_on_{f.kind}:{f.id}")
            if f.kind in {"select", "radio"} and f.options and str(s.value) not in f.options:
                errs.append(f"option_not_offered:{f.id}:{s.value}")
        elif s.op == CLICK:
            if f.kind != "button":
                errs.append(f"click_on_{f.kind}:{f.id}")
            if f.submit and not approve_submit:
                gated.append(f.id)
        elif s.op == SKIP and f.required and s.reason != "secret_human":
            errs.append(f"required_unfilled:{f.id}:{s.reason}")
    for f in fields:
        if seen.get(f.id, 0) != 1:
            errs.append(f"coverage:{f.id}:{seen.get(f.id, 0)}")
    submits = [s for s in plan if s.op == CLICK and by_id.get(s.field_id) and by_id[s.field_id].submit]
    if len(submits) > 1:
        errs.append("multiple_submits")
    return Validation(ok=not errs, errors=errs, gated_submit=gated)


def order_plan(plan: list[Step], fields: list[Field]) -> list[Step]:
    """FILL → CHECK → CLICK (DOM order within each), submit last, SKIPs dropped."""
    by_id = {f.id: f for f in fields}
    live = [s for s in plan if s.op != SKIP]

    def key(s: Step) -> tuple[int, int, int]:
        f = by_id[s.field_id]
        return (1 if f.submit else 0, _ORDER[s.op], f.dom_index)

    return sorted(live, key=key)


def compile_comet(
    plan: list[Step], fields: list[Field], document: dict[str, Any], *, approve_submit: bool = False
) -> dict[str, Any]:
    """Validated plan → ONE comet-control send body. Submit only when approved."""
    v = validate_plan(plan, fields, document, approve_submit=approve_submit)
    if not v.ok:
        return {"ok": False, "error": "plan_invalid", "errors": v.errors}
    by_id = {f.id: f for f in fields}
    actions: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    pending_submit = None
    for s in order_plan(plan, fields):
        f = by_id[s.field_id]
        loc = {"by": "label", "label": f.label}
        if s.op == FILL:
            # No expect.value: page_context caps inputs (8 compact), so later fields false-fail.
            # The locator fill result echoes element.value; `checks` compares it after the send.
            checks.append({"action_index": len(actions), "field": f.id, "value": s.value})
            actions.append({"type": "locator", "locator": loc, "operation": "fill", "value": s.value})
        elif s.op == CHECK and f.kind == "select":
            actions.append({"type": "locator", "locator": loc, "operation": "select_option", "value": s.value})
        elif s.op == CHECK and f.kind == "radio":
            # A radio group is chosen by the option's own label, not the group legend.
            actions.append({"type": "locator", "locator": {"by": "label", "label": str(s.value)}, "operation": "check"})
        elif s.op == CHECK:
            actions.append({"type": "locator", "locator": loc, "operation": "set_checked", "checked": bool(s.value)})
        elif s.op == CLICK:
            act = {"type": "locator", "locator": {"by": "role", "role": "button", "name": f.label, "exact": True},
                   "operation": "click"}
            if f.submit and not approve_submit:
                pending_submit = f.label
                continue
            actions.append(act)
    actions.append({"type": "page_context", "sections": ["inputs", "buttons"]})
    out: dict[str, Any] = {"ok": True, "body": {"actions": actions}, "action_count": len(actions), "checks": checks}
    if pending_submit:
        out["pending_submit"] = pending_submit
        out["hint"] = "Submit is approval-gated: show the filled form to the user, then send the submit click."
    return out


def verify_fills(compiled: dict[str, Any], response: dict[str, Any]) -> list[str]:
    """Compare each fill's echoed element.value with the plan. Empty list = all fills landed."""
    results = (response.get("response") or response).get("results") or []
    misses = []
    for c in compiled.get("checks", []):
        got = results[c["action_index"]].get("value") if c["action_index"] < len(results) else None
        if got != c["value"]:
            misses.append(f"fill_mismatch:{c['field']}")
    return misses


def compile_cua(
    plan: list[Step], fields: list[Field], document: dict[str, Any], app: str, *, approve_submit: bool = False
) -> dict[str, Any]:
    """Validated plan → ONE macos-cua act payload. Native macOS forms only."""
    v = validate_plan(plan, fields, document, approve_submit=approve_submit)
    if not v.ok:
        return {"ok": False, "error": "plan_invalid", "errors": v.errors}
    by_id = {f.id: f for f in fields}
    steps: list[dict[str, Any]] = []
    pending_submit = None
    for s in order_plan(plan, fields):
        f = by_id[s.field_id]
        if s.op == FILL:
            steps.append({"action": "set_value", "label": f.label, "value": s.value})
        elif s.op == CHECK:
            steps.append({"action": "click", "label": str(s.value) if f.kind in {"radio", "select"} else f.label})
        elif s.op == CLICK:
            if f.submit and not approve_submit:
                pending_submit = f.label
                continue
            steps.append({"action": "click", "label": f.label})
    fills = [s for s in order_plan(plan, fields) if s.op == FILL]
    expect = {"value": {"label": by_id[fills[-1].field_id].label, "equals": fills[-1].value}} if fills else None
    out: dict[str, Any] = {"ok": True, "act": {"app": app, "steps": steps, **({"expect": expect} if expect else {})}}
    if pending_submit:
        out["pending_submit"] = pending_submit
    return out


def _fields_from(raw: list[dict[str, Any]]) -> list[Field]:
    return [Field(str(f["id"]), str(f["label"]), str(f["kind"]), bool(f.get("required", False)),
                  tuple(f.get("options", ())), int(f.get("dom_index", i))) for i, f in enumerate(raw)]


def main(argv: list[str] | None = None) -> int:
    """compile: fields + document JSON -> one comet-control send body (stdout). Exit 64 when invalid."""
    ap = argparse.ArgumentParser(description="One-pass FILL/CHECK/CLICK/SKIP form plan -> one comet-control send")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile")
    c.add_argument("--fields", required=True, help="JSON file: [{id,label,kind,required?,options?,dom_index?}]")
    c.add_argument("--doc", required=True, help="JSON file: {source key: value}")
    c.add_argument("--aliases", help="JSON file: {doc key: [label aliases]}")
    c.add_argument("--approve-submit", action="store_true", help="only after the user approved this submit")
    a = ap.parse_args(argv)
    load = lambda path: json.load(open(path))
    fields = _fields_from(load(a.fields))
    doc = load(a.doc)
    plan = plan_form(fields, doc, aliases=load(a.aliases) if a.aliases else None)
    out = compile_comet(plan, fields, doc, approve_submit=a.approve_submit)
    out["plan"] = [{"op": s.op, "field": s.field_id, **({"reason": s.reason} if s.reason else {})} for s in plan]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 64


if __name__ == "__main__":
    sys.exit(main())
