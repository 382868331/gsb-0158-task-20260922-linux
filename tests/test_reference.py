"""Cross-check the CEK core against the independent small-step reference.

The reference never calls the core; expected values come solely from the
reference reducer.  Hand-written cases plus a family of deterministically
generated (seeded) well-typed programs are compared on value, and on
error class where reduction is provably impossible (unmatched prompt).
"""

import random
import unittest

from delimcc import (
    Add,
    Call,
    Int,
    Lam,
    NoPromptError,
    Prompt,
    Shift,
    Var,
)
from delimcc import reference as ref
from delimcc.core import run as core_run

HAND_PROGRAMS = [
    Int(7),
    Add(Int(1), Int(2)),
    Call(Lam("x", Add(Var("x"), Int(1))), Int(41)),
    Prompt(0, Add(Int(1), Shift(0, "k", Call(Var("k"), Int(100))))),
    Prompt(
        0,
        Add(
            Int(5),
            Shift(0, "k", Add(Call(Var("k"), Int(4)), Call(Var("k"), Int(6)))),
        ),
    ),
    Prompt(
        1,
        Add(
            Int(100),
            Prompt(
                2,
                Add(
                    Int(20),
                    Shift(
                        2,
                        "k2",
                        Shift(1, "k1", Call(Var("k1"), Call(Var("k2"), Int(5)))),
                    ),
                ),
            ),
        ),
    ),
    Prompt(1, Add(Int(100), Prompt(2, Add(Int(20), Shift(1, "k", Call(Var("k"), Int(7))))))),
    Prompt(
        0,
        Add(
            Int(2),
            Shift(
                0,
                "k",
                Add(
                    Add(Call(Var("k"), Int(1)), Call(Var("k"), Int(10))),
                    Call(Var("k"), Int(100)),
                ),
            ),
        ),
    ),
    # shift that never invokes its continuation
    Prompt(7, Add(Int(1), Shift(7, "k", Int(999)))),
    # continuation escapes the prompt inside a returned lambda... not in
    # this language (body result position only), but k can be passed as
    # an argument to another lambda:
    Prompt(
        3,
        Call(
            Lam("f", Call(Var("f"), Int(2))),
            Lam("a", Add(Int(1), Shift(3, "k", Call(Var("k"), Var("a"))))),
        ),
    ),
]

UNMATCHED_PROGRAMS = [
    Shift(0, "k", Int(0)),
    Prompt(1, Shift(2, "k", Int(0))),
    Add(Int(1), Prompt(1, Shift(0, "k", Int(0)))),
]


# --------------------------------------------------------------------------- #
# Deterministic well-typed program generator                                   #
# --------------------------------------------------------------------------- #

NAME_POOL = ("a", "b", "c")  # small pool on purpose: exercises shadowing


class Generator:
    """Generates int-producing terms; F-bindings are int->int functions."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.uniq = 0

    def _fresh(self) -> str:
        self.uniq += 1
        return f"{NAME_POOL[self.uniq % len(NAME_POOL)]}{self.uniq}"

    def gen_int(self, depth: int, int_vars: tuple[str, ...],
                fun_vars: tuple[str, ...], tags: tuple[int, ...]) -> object:
        # Only genuinely non-recursing forms are available at depth 0;
        # every compound form consumes one unit of depth, so generation
        # always terminates and the resulting tree is a full tree of <= depth.
        choices = ["lit"]
        if int_vars:
            choices.append("var")
        if depth > 0:
            choices += ["add", "app", "prompt", "shift", "lam-app"]
            if fun_vars:
                choices.append("appvar")
        pick = self.rng.choice(choices)

        if pick == "lit":
            return Int(self.rng.randint(-5, 5))
        if pick == "var":
            return Var(self.rng.choice(int_vars))
        if pick == "add":
            return Add(
                self.gen_int(depth - 1, int_vars, fun_vars, tags),
                self.gen_int(depth - 1, int_vars, fun_vars, tags),
            )
        if pick == "app":
            p = self._fresh()
            fn = Lam(p, self.gen_int(depth - 1, int_vars + (p,), fun_vars, tags))
            return Call(fn, self.gen_int(depth - 1, int_vars, fun_vars, tags))
        if pick == "appvar":
            f = self.rng.choice(fun_vars)
            return Call(Var(f), self.gen_int(depth - 1, int_vars, fun_vars, tags))
        if pick == "prompt":
            return Prompt(
                self.rng.choice(tags),
                self.gen_int(depth - 1, int_vars, fun_vars, tags),
            )
        if pick == "shift":
            tag = self.rng.choice(tags)
            k = self._fresh()
            # Shifts may be unmatched (no same-tag prompt around); both
            # implementations must agree on that failure too.
            return Shift(
                tag,
                k,
                self.gen_int(depth - 1, int_vars, fun_vars + (k,), tags),
            )
        # lam-app: ((lam p. body) arg)
        p = self._fresh()
        body = self.gen_int(depth - 1, int_vars + (p,), fun_vars, tags)
        arg = self.gen_int(depth - 1, int_vars, fun_vars, tags)
        return Call(Lam(p, body), arg)


def gen_programs(seed: int, count: int):
    rng = random.Random(seed)
    out = []
    for i in range(count):
        g = Generator(rng)
        depth = rng.randint(1, 4)
        ntags = rng.randint(1, 3)
        tags = tuple(range(ntags))
        out.append(g.gen_int(depth, (), (), tags))
    return out


class TestReferenceAgreement(unittest.TestCase):
    def _assert_agree(self, program):
        r_ref = ref.reduce_program(program)
        r_core = core_run(program)
        self.assertEqual(
            r_ref.descriptor(),
            _core_descriptor(r_core.value),
            msg=f"value mismatch on {program!r}",
        )
        # Event subsequence agreement (core also emits prompt-enter, which
        # the reduction-style reference has no counterpart for).
        core_events = [(e.kind, e.tag) for e in r_core.events
                       if e.kind != "prompt-enter"]
        self.assertEqual(core_events, list(r_ref.events),
                         msg=f"event mismatch on {program!r}")

    def test_hand_written_programs(self):
        for p in HAND_PROGRAMS:
            with self.subTest(program=repr(p)):
                self._assert_agree(p)

    def test_unmatched_programs_both_fail(self):
        for p in UNMATCHED_PROGRAMS:
            with self.subTest(program=repr(p)):
                with self.assertRaises(NoPromptError):
                    ref.reduce_program(p)
                with self.assertRaises(NoPromptError):
                    core_run(p)

    def test_generated_programs_agree(self):
        programs = gen_programs(seed=20260922, count=200)
        matched = unmatched = 0
        for p in programs:
            with self.subTest(program=repr(p)):
                try:
                    self._assert_agree(p)
                    matched += 1
                except NoPromptError:
                    with self.assertRaises(NoPromptError):
                        core_run(p)
                    unmatched += 1
        # sanity: the generator actually exercised both paths
        self.assertGreater(matched, 100)
        self.assertGreater(unmatched, 5)


def _core_descriptor(v):
    from delimcc.core import IntVal, Closure, ContVal
    if isinstance(v, IntVal):
        return ("int", v.value)
    if isinstance(v, Closure):
        return ("closure", v.param)
    if isinstance(v, ContVal):
        return ("continuation", v.tag, len(v.frames))
    raise AssertionError(f"unexpected value {v!r}")


if __name__ == "__main__":
    unittest.main()
