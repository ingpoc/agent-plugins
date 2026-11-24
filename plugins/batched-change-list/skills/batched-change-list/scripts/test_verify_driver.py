import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_driver


class VerifyDriverTests(unittest.TestCase):
    def test_shared_check_gates_lane_checks(self):
        calls = []
        original = verify_driver.run_check
        verify_driver.run_check = lambda command, **kwargs: calls.append(tuple(command)) or 0
        try:
            results, cleanup = verify_driver.run_round(shared=["shared"], lanes=[["lane"]])
        finally:
            verify_driver.run_check = original
        self.assertEqual(calls, [("shared",), ("lane",)])
        self.assertEqual([result.verdict for result in results], ["passed"])
        self.assertIsNone(cleanup)

    def test_lane_checks_and_judging(self):
        original = verify_driver.run_check
        codes = iter((0, 5))
        verify_driver.run_check = lambda command, **kwargs: next(codes)
        try:
            results, _ = verify_driver.run_round(shared=None, lanes=[["good"], ["bad"]])
        finally:
            verify_driver.run_check = original
        self.assertEqual([result.verdict for result in results], ["passed", "failed"])

    def test_shared_failure_skips_lanes(self):
        original = verify_driver.run_check
        calls = []
        verify_driver.run_check = lambda command, **kwargs: calls.append(tuple(command)) or 9
        try:
            results, code = verify_driver.run_round(shared=["shared"], lanes=[["lane"]])
        finally:
            verify_driver.run_check = original
        self.assertEqual(calls, [("shared",)])
        self.assertEqual(results, [])
        self.assertEqual(code, 9)

    def test_cleanup_failure_does_not_flip_pass(self):
        def fail_cleanup():
            raise RuntimeError("teardown failed")
        original = verify_driver.run_check
        verify_driver.run_check = lambda command, **kwargs: 0
        try:
            results, cleanup = verify_driver.run_round(shared=None, lanes=[["lane"]], cleanup=fail_cleanup)
        finally:
            verify_driver.run_check = original
        self.assertEqual(results[0].verdict, "passed")
        self.assertEqual(results[0].cleanup, "failed")
        self.assertEqual(cleanup, 1)

    def test_self_test_detects_broken_judging_invariant(self):
        original = verify_driver.judge
        verify_driver.judge = lambda code: "passed"
        try:
            with self.assertRaises(AssertionError):
                verify_driver.self_test()
        finally:
            verify_driver.judge = original

    def test_self_test_rejects_broken_cleanup_invariant(self):
        original = verify_driver.run_round
        verify_driver.run_round = lambda **kwargs: ([verify_driver.CheckResult("passed", 0, "passed")], None)
        try:
            with self.assertRaises(AssertionError):
                verify_driver.self_test()
        finally:
            verify_driver.run_round = original

    def test_self_test_rejects_skipped_shared_check(self):
        original = verify_driver.run_round

        def skip_shared(**kwargs):
            kwargs["shared"] = None
            return original(**kwargs)

        verify_driver.run_round = skip_shared
        try:
            with self.assertRaises(AssertionError):
                verify_driver.self_test()
        finally:
            verify_driver.run_round = original

    def test_self_test_rejects_runner_that_skips_command_but_reports_success(self):
        original = verify_driver.run_check
        verify_driver.run_check = lambda command, **kwargs: 0
        try:
            with self.assertRaises(AssertionError):
                verify_driver.self_test()
        finally:
            verify_driver.run_check = original


if __name__ == "__main__":
    unittest.main()
