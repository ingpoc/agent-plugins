import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "comet-control" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from form_plan import (CHECK, CLICK, FILL, SKIP, Field, Step, compile_comet, compile_cua,
                       order_plan, plan_form, validate_plan, verify_fills)

FIELDS = [
    Field("f1", "Full name", "text", required=True, dom_index=0),
    Field("f2", "Email address", "email", required=True, dom_index=1),
    Field("f3", "Nationality", "select", required=True, options=("Indian", "Other"), dom_index=2),
    Field("f4", "I agree to the terms", "checkbox", required=True, dom_index=3),
    Field("f5", "Password", "password", dom_index=4),
    Field("f6", "Middle name", "text", dom_index=5),
    Field("f7", "Submit application", "button", dom_index=7),
    Field("f8", "Save draft", "button", dom_index=6),
]
DOC = {"full name": "Ada Lovelace", "email": "ada@example.com", "nationality": "Indian", "agree": True,
       "password": "hunter2"}
ALIASES = {"agree": ["I agree"]}


class FormPlanTests(unittest.TestCase):
    def plan(self, **kw):
        return plan_form(FIELDS, DOC, aliases=ALIASES, **kw)

    def test_one_op_per_field(self):
        p = self.plan()
        self.assertEqual(sorted(s.field_id for s in p), sorted(f.id for f in FIELDS))
        ops = {s.field_id: s.op for s in p}
        self.assertEqual(ops, {"f1": FILL, "f2": FILL, "f3": CHECK, "f4": CHECK, "f5": SKIP, "f6": SKIP,
                               "f7": CLICK, "f8": SKIP})

    def test_secret_never_in_plan_or_body(self):
        p = self.plan()
        self.assertEqual(next(s for s in p if s.field_id == "f5").reason, "secret_human")
        body = compile_comet(p, FIELDS, DOC)
        self.assertNotIn("hunter2", json.dumps(body))

    def test_submit_gated_by_default(self):
        out = compile_comet(self.plan(), FIELDS, DOC)
        self.assertTrue(out["ok"])
        self.assertEqual(out["pending_submit"], "Submit application")
        self.assertNotIn("Submit application", json.dumps(out["body"]))

    def test_submit_last_when_approved(self):
        out = compile_comet(self.plan(), FIELDS, DOC, approve_submit=True)
        acts = out["body"]["actions"]
        self.assertEqual(acts[-2]["locator"]["name"], "Submit application")
        self.assertEqual(acts[-1]["type"], "page_context")

    def test_order_fill_check_click(self):
        ordered = order_plan(self.plan(click_labels=("Save draft",)), FIELDS)
        self.assertEqual([s.op for s in ordered], [FILL, FILL, CHECK, CHECK, CLICK, CLICK])
        self.assertEqual(ordered[-1].field_id, "f7")

    def test_single_batched_send(self):
        out = compile_comet(self.plan(), FIELDS, DOC)
        self.assertEqual(out["action_count"], 5)  # 2 fills + select + checkbox + page_context
        fills = [a for a in out["body"]["actions"] if a.get("operation") == "fill"]
        self.assertTrue(all("expect" not in a for a in fills))
        self.assertEqual([(c["action_index"], c["value"]) for c in out["checks"]], [(0, "Ada Lovelace"), (1, "ada@example.com")])
        good = {"response": {"results": [{"value": "Ada Lovelace"}, {"value": "ada@example.com"}]}}
        self.assertEqual(verify_fills(out, good), [])
        self.assertEqual(verify_fills(out, {"results": [{"value": "Ada"}, {"value": "ada@example.com"}]}), ["fill_mismatch:f1"])

    def test_required_skip_fails_closed(self):
        doc = dict(DOC); doc.pop("email")
        p = plan_form(FIELDS, doc, aliases=ALIASES)
        out = compile_comet(p, FIELDS, doc)
        self.assertFalse(out["ok"])
        self.assertIn("required_unfilled:f2:no_source_value", out["errors"])

    def test_invented_value_rejected(self):
        p = [Step(FILL, "f1", "Grace Hopper") if s.field_id == "f1" else s for s in self.plan()]
        v = validate_plan(p, FIELDS, DOC)
        self.assertIn("invented_value:f1", v.errors)

    def test_option_must_be_offered(self):
        p = [Step(CHECK, "f3", "Martian") if s.field_id == "f3" else s for s in self.plan()]
        self.assertIn("option_not_offered:f3:Martian", validate_plan(p, FIELDS, DOC).errors)

    def test_coverage_and_type_errors(self):
        p = [s for s in self.plan() if s.field_id != "f6"] + [Step(FILL, "f4", "True")]
        errs = validate_plan(p, FIELDS, DOC).errors
        self.assertIn("coverage:f6:0", errs)
        self.assertIn("fill_on_checkbox:f4", errs)

    def test_ambiguous_label_skips(self):
        fields = [Field("x", "Name", "text")]
        p = plan_form(fields, {"name": "A", "first name": "B"}, aliases={"first name": ["name"]})
        self.assertEqual((p[0].op, p[0].reason), (SKIP, "ambiguous"))

    def test_cua_compile_native_only_shape(self):
        out = compile_cua(self.plan(), FIELDS, DOC, app="System Settings")
        steps = out["act"]["steps"]
        self.assertEqual(steps[0], {"action": "set_value", "label": "Full name", "value": "Ada Lovelace"})
        self.assertEqual(out["act"]["expect"]["value"]["equals"], "ada@example.com")
        self.assertEqual(out["pending_submit"], "Submit application")

    def test_radio_uses_option_label_and_checkbox_set_checked(self):
        fields = [Field("s", "Pizza Size", "radio", options=("Small", "Medium"), dom_index=0),
                  Field("b", "Bacon", "checkbox", dom_index=1), Field("t", "Delivery time", "time", dom_index=2)]
        doc = {"pizza size": "Medium", "bacon": False, "delivery time": "18:30"}
        acts = compile_comet(plan_form(fields, doc), fields, doc)["body"]["actions"]
        self.assertIn({"type": "locator", "locator": {"by": "label", "label": "Medium"}, "operation": "check"}, acts)
        self.assertIn({"type": "locator", "locator": {"by": "label", "label": "Bacon"}, "operation": "set_checked",
                       "checked": False}, acts)
        self.assertEqual(acts[0]["operation"], "fill")

    def test_cli_compile_one_body_and_exit_64_when_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            fields = Path(d, "f.json"); doc = Path(d, "d.json")
            fields.write_text(json.dumps([{"id": "n", "label": "Full name", "kind": "text", "required": True},
                                          {"id": "go", "label": "Submit", "kind": "button"}]))
            doc.write_text(json.dumps({"full name": "Ada"}))
            run = lambda: subprocess.run([sys.executable, str(SCRIPTS / "form_plan.py"), "compile", "--fields", str(fields),
                                          "--doc", str(doc)], capture_output=True, text=True)
            ok = run()
            self.assertEqual(ok.returncode, 0, ok.stderr)
            out = json.loads(ok.stdout)
            self.assertEqual(out["pending_submit"], "Submit")
            self.assertEqual(len(out["body"]["actions"]), 2)
            doc.write_text(json.dumps({}))
            bad = run()
            self.assertEqual(bad.returncode, 64)
            self.assertIn("required_unfilled:n:no_source_value", json.loads(bad.stdout)["errors"])


if __name__ == "__main__":
    unittest.main()
