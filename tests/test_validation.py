"""Input validation, locatable errors, and interface limits."""

import math
import unittest

from delimcc import (
    Add,
    BadAstError,
    Call,
    Int,
    Lam,
    NodeLimitError,
    Prompt,
    Shift,
    StepLimitError,
    Var,
    count_nodes,
    evaluate,
    validate,
)
from delimcc import reference as ref


class TestValidation(unittest.TestCase):
    def test_reject_float_nan(self):
        # dataclass construction accepts the float; validation is the
        # boundary that rejects NaN with a locatable error.
        with self.assertRaises(BadAstError) as cm:
            validate(Int(float("nan")))
        self.assertEqual(cm.exception.code, "bad-ast")
        self.assertIn("NaN", str(cm.exception))
        self.assertTrue(cm.exception.loc)

    def test_reject_nan_at_validation(self):
        node = Int(float("nan"))
        with self.assertRaises(BadAstError) as cm:
            validate(node)
        self.assertEqual(cm.exception.code, "bad-ast")
        self.assertIn("NaN", str(cm.exception))
        self.assertTrue(cm.exception.loc)

    def test_reject_infinity(self):
        for x in (float("inf"), float("-inf")):
            with self.subTest(x=x):
                with self.assertRaises(BadAstError) as cm:
                    validate(Int(x))
                self.assertIn("Infinity", str(cm.exception))

    def test_reject_plain_float(self):
        with self.assertRaises(BadAstError) as cm:
            validate(Int(1.5))
        self.assertIn("int", str(cm.exception))

    def test_reject_bool_for_integer(self):
        # bool is an int subclass in Python; the contract rejects it.
        with self.assertRaises(BadAstError) as cm:
            validate(Int(True))
        self.assertIn("bool", str(cm.exception).lower())
        with self.assertRaises(BadAstError):
            validate(Prompt(False, Int(1)))
        with self.assertRaises(BadAstError):
            validate(Shift(True, "k", Int(0)))

    def test_reject_bool_tag(self):
        with self.assertRaises(BadAstError):
            validate(Shift(False, "k", Int(0)))

    def test_reject_empty_and_nonstr_name(self):
        with self.assertRaises(BadAstError):
            validate(Var(""))
        with self.assertRaises(BadAstError):
            validate(Lam(3, Int(1)))

    def test_reject_non_node_child(self):
        with self.assertRaises(BadAstError):
            validate(Add(Int(1), 2))
        with self.assertRaises(BadAstError):
            validate(Call("notanode", Int(1)))

    def test_reject_non_node_root(self):
        with self.assertRaises(BadAstError):
            validate(42)  # type: ignore[arg-type]

    def test_error_loc_is_a_path(self):
        # Add.right is a Prompt whose body is a bool-tagged Shift.
        bad = Add(Int(1), Prompt(True, Int(0)))
        with self.assertRaises(BadAstError) as cm:
            validate(bad)
        loc = cm.exception.loc
        self.assertEqual(loc[0], "program")
        self.assertIn("Prompt", loc[-1])

    def test_no_silent_correction(self):
        # 1.999 must not be rounded to 2; it must be rejected outright.
        with self.assertRaises(BadAstError):
            evaluate(Add(Int(1), Int(1.999)))


class TestLimits(unittest.TestCase):
    def test_count_nodes(self):
        self.assertEqual(count_nodes(Int(1)), 1)
        self.assertEqual(count_nodes(Add(Int(1), Int(2))), 3)
        tree = Lam("x", Add(Var("x"), Int(1)))
        self.assertEqual(count_nodes(tree), 4)

    def test_node_limit_enforced(self):
        # a chain with > 5 nodes against a 5-node budget
        e = Add(Add(Int(1), Int(2)), Add(Int(3), Int(4)))  # 7 nodes
        with self.assertRaises(NodeLimitError) as cm:
            evaluate(e, max_nodes=5)
        self.assertEqual(cm.exception.code, "node-limit")

    def test_node_boundary_accepted(self):
        e = Add(Add(Int(1), Int(2)), Int(3))  # 5 nodes
        self.assertEqual(evaluate(e, max_nodes=5).as_int(), 6)

    def test_default_node_limit_1000(self):
        # Build exactly 1000 nodes: (((0+1)+2)+...)+499 => 999 nodes... use
        # a left chain of n Ints: nodes = 2n-1. n=500 -> 999; add one more
        # Int under an Add wrapper to reach 1000.
        e = Int(0)
        for _ in range(499):
            e = Add(e, Int(1))
        self.assertEqual(count_nodes(e), 999)
        e = Add(e, Int(1))
        self.assertEqual(count_nodes(e), 1001)
        with self.assertRaises(NodeLimitError):
            evaluate(e)

    def test_step_limit_is_budget_not_proof(self):
        # A multi-shot term big enough to need more than a tiny budget.
        # prompt0 { 1 + shift0 k. (k 1) + (k 2) + ... } via nesting:
        body = Int(0)
        for i in range(1, 40):
            body = Add(body, Call(Var("k"), Int(i)))
        e = Prompt(0, Add(Int(1), Shift(0, "k", body)))
        with self.assertRaises(StepLimitError) as cm:
            evaluate(e, max_steps=10)
        self.assertEqual(cm.exception.code, "step-limit")
        self.assertIn("budget", str(cm.exception))

    def test_step_limit_default_is_20000_and_result_within(self):
        # Same program finishes comfortably under the real budget.
        body = Int(0)
        for i in range(1, 40):
            body = Add(body, Call(Var("k"), Int(i)))
        e = Prompt(0, Add(Int(1), Shift(0, "k", body)))
        r = evaluate(e)
        # each k(i) resumes prompt{1 + i} = i+1 for i=1..39; body sums them
        self.assertEqual(r.as_int(), sum(i + 1 for i in range(1, 40)))
        self.assertLessEqual(r.steps, 20000)

    def test_no_prompt_is_distinct_from_budget(self):
        # The proven-failure error must surface even with a generous budget.
        with self.assertRaises(Exception) as cm_core:
            evaluate(Shift(5, "k", Int(0)), max_steps=20000)
        with self.assertRaises(Exception) as cm_ref:
            ref.reduce_program(Shift(5, "k", Int(0)), max_steps=20000)
        from delimcc import NoPromptError
        self.assertIsInstance(cm_core.exception, NoPromptError)
        self.assertIsInstance(cm_ref.exception, NoPromptError)

    def test_bad_budget_arguments(self):
        with self.assertRaises(BadAstError):
            evaluate(Int(1), max_steps=0)
        with self.assertRaises(BadAstError):
            evaluate(Int(1), max_steps=True)
        with self.assertRaises(BadAstError):
            evaluate(Int(1), max_nodes=0)


if __name__ == "__main__":
    unittest.main()
