"""Basic language semantics: integers, addition, lambdas, calls."""

import unittest

from delimcc import (
    Add,
    BadAstError,
    Call,
    Int,
    Lam,
    UnboundVarError,
    Var,
    evaluate,
)


def lam(p, b):
    return Lam(p, b)


def add(a, b):
    return Add(a, b)


def call(f, a):
    return Call(f, a)


class TestBasic(unittest.TestCase):
    def test_int(self):
        self.assertEqual(evaluate(Int(42)).as_int(), 42)
        self.assertEqual(evaluate(Int(0)).as_int(), 0)
        self.assertEqual(evaluate(Int(-7)).as_int(), -7)

    def test_bigint_exact(self):
        # Python ints are arbitrary precision; results are exact.
        n = 10 ** 40
        r = evaluate(add(Int(n), Int(1))).as_int()
        self.assertEqual(r, n + 1)

    def test_add_chain(self):
        e = add(add(Int(1), Int(2)), add(Int(10), Int(20)))
        self.assertEqual(evaluate(e).as_int(), 33)

    def test_identity(self):
        e = call(lam("x", Var("x")), Int(5))
        self.assertEqual(evaluate(e).as_int(), 5)

    def test_closure_captures_lexical_env(self):
        # (lam a. lam b. a + b)(3)(4)
        e = call(
            call(lam("a", lam("b", add(Var("a"), Var("b")))), Int(3)),
            Int(4),
        )
        self.assertEqual(evaluate(e).as_int(), 7)

    def test_shadowing(self):
        # (lam x. (lam x. x) 2) 1  == 2
        e = call(lam("x", call(lam("x", Var("x")), Int(2))), Int(1))
        self.assertEqual(evaluate(e).as_int(), 2)

    def test_higher_order(self):
        # (lam f. f(f(1))) (lam x. x + 10) == 21
        e = call(
            lam("f", call(Var("f"), call(Var("f"), Int(1)))),
            lam("x", add(Var("x"), Int(10))),
        )
        self.assertEqual(evaluate(e).as_int(), 21)

    def test_argument_evaluated_call_by_value(self):
        # argument is a computed expression, not substituted unevaluated
        e = call(lam("x", add(Var("x"), Var("x"))), add(Int(2), Int(3)))
        self.assertEqual(evaluate(e).as_int(), 10)

    def test_unbound_variable(self):
        with self.assertRaises(UnboundVarError) as cm:
            evaluate(Var("missing"))
        self.assertEqual(cm.exception.code, "unbound-var")
        self.assertIn("missing", str(cm.exception))

    def test_add_requires_ints_left(self):
        with self.assertRaises(BadAstError):
            evaluate(add(lam("x", Var("x")), Int(1)))

    def test_add_requires_ints_right(self):
        with self.assertRaises(BadAstError):
            evaluate(add(Int(1), lam("x", Var("x"))))

    def test_call_requires_function(self):
        with self.assertRaises(BadAstError):
            evaluate(call(Int(1), Int(2)))

    def test_result_metadata(self):
        r = evaluate(add(Int(1), Int(2)))
        self.assertEqual(r.as_int(), 3)
        self.assertEqual(r.node_count, 3)
        self.assertGreater(r.steps, 0)


if __name__ == "__main__":
    unittest.main()
