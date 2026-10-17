"""Default book.py path stops before Pay & Book. The click needs --confirm-pay."""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

BOOK = Path(__file__).resolve().parents[1] / "skills/irctc-train-booking/scripts/book.py"
BASE = ["NDLS", "MMCT", "17/10/2026", "TATKAL", "AC 3 Tier (3A)", "12951", "PREFIX"]


def load_book():
    spec = importlib.util.spec_from_file_location("irctc_book_pay_gate", BOOK)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {BOOK}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PayGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = load_book()
        cls.tree = ast.parse(BOOK.read_text())

    def test_default_path_never_clicks_pay_and_book(self):
        args, at, confirm = self.book.parse_book_args(BASE + ["--at", "10:00:00"])
        self.assertEqual(args, BASE)
        self.assertEqual(at, "10:00:00")
        self.assertFalse(confirm)
        self.assertIsNone(self.book.pay_book_actions(confirm))
        self.assertIsNone(self.book.pay_book_actions(False))

        clicks = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Constant) and node.value == "Pay & Book"
        ]
        self.assertEqual(len(clicks), 1, "Pay & Book click text must exist once, inside the gate")
        gate = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == "pay_book_actions")
        self.assertTrue(any(isinstance(n, ast.Constant) and n.value == "Pay & Book" for n in ast.walk(gate)))
        self.assertLess(clicks[0].lineno, gate.end_lineno)
        self.assertGreaterEqual(clicks[0].lineno, gate.lineno)

        main = next(node for node in self.tree.body if isinstance(node, ast.If))
        self.assertIsInstance(main.test, ast.Compare)
        sends = [
            node
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "send"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "actions"
        ]
        self.assertEqual(len(sends), 1)
        stop = next(
            node
            for node in ast.walk(main)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "actions"
        )
        self.assertEqual(stop.body[0].value.func.id, "log")
        self.assertIn(sends[0], ast.walk(ast.Module(body=stop.orelse, type_ignores=[])))

    def test_confirm_pay_clicks_gateway_only(self):
        args, at, confirm = self.book.parse_book_args(["--confirm-pay", *BASE, "--at", "11:00:00"])
        self.assertTrue(confirm)
        self.assertEqual(at, "11:00:00")
        self.assertEqual(args, BASE)
        actions = self.book.pay_book_actions(True)
        self.assertEqual([item.get("text") for item in actions if item.get("type") == "click_text"], ["Pay & Book"])
        blob = " ".join(str(item) for item in actions).lower()
        for banned in ("google pay", "pay now", "upi", "card"):
            self.assertNotIn(banned, blob)


if __name__ == "__main__":
    unittest.main()
