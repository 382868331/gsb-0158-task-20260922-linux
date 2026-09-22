"""Delimited-continuation semantics: prompts, shift, multi-shot resume."""

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
    evaluate,
)


def add(a, b):
    return Add(a, b)


def call(f, a):
    return Call(f, a)


def lam(p, b):
    return Lam(p, b)


def prompt(tag, body):
    return Prompt(tag, body)


def shift(tag, k, body):
    return Shift(tag, k, body)


class TestDelimitedControl(unittest.TestCase):
    def test_shift_without_resume_discards_context(self):
        # prompt0 { 1 + shift0 k. 100 } == 100 (the "1 + []" context is dropped)
        e = prompt(0, add(Int(1), shift(0, "k", Int(100))))
        self.assertEqual(evaluate(e).as_int(), 100)

    def test_non_local_return_shape(self):
        # prompt0 { (lam x. 10 + shift0 k. x) (20 + 30) } == 50
        # the continuation "10 + []" is abandoned; body returns x = 50.
        e = prompt(
            0,
            call(
                lam("x", add(Int(10), shift(0, "k", Var("x")))),
                add(Int(20), Int(30)),
            ),
        )
        self.assertEqual(evaluate(e).as_int(), 50)

    def test_resume_once(self):
        # prompt0 { 1 + shift0 k. k(100) }
        # captured context F = 1 + [] ; k(100) = prompt0 { 1 + 100 } = 101
        e = prompt(
            0,
            add(Int(1), shift(0, "k", call(Var("k"), Int(100)))),
        )
        self.assertEqual(evaluate(e).as_int(), 101)

    def test_capture_right_side_of_addition(self):
        # Required case: the shift sits in the RIGHT operand and captures
        # "5 + []"; left side 5 has already been evaluated.
        # prompt0 { 5 + (shift0 k. k(k(4))) *via* add nesting }
        # k(4)  -> prompt0 { 5 + 4 } = 9; outer 9 + k(4 again)? Build:
        # prompt0 { 5 + shift0 k. k(4) + k(6) }
        # captured F = 5 + []
        # body: (k 4) + (k 6); each resume re-enters its own prompt0
        #   k(4) = 9, k(6) = 11, body sum = 20
        e = prompt(
            0,
            add(
                Int(5),
                shift(
                    0,
                    "k",
                    add(call(Var("k"), Int(4)), call(Var("k"), Int(6))),
                ),
            ),
        )
        r = evaluate(e)
        self.assertEqual(r.as_int(), 20)
        kinds = [(ev.kind, ev.tag) for ev in r.events]
        self.assertEqual(kinds.count(("shift", 0)), 1)
        self.assertEqual(kinds.count(("resume", 0)), 2)

    def test_repeated_invocation_multi_shot(self):
        # Classic multi-shot:
        # prompt0 { 2 + shift0 k. (k 1) + (k 10) + (k 100) }
        # F = 2 + [] ; resumes give 3, 12, 102; body sums them = 117
        e = prompt(
            0,
            add(
                Int(2),
                shift(
                    0,
                    "k",
                    add(
                        add(call(Var("k"), Int(1)), call(Var("k"), Int(10))),
                        call(Var("k"), Int(100)),
                    ),
                ),
            ),
        )
        self.assertEqual(evaluate(e).as_int(), 117)

    def test_nested_distinct_tags(self):
        # Required case: nested prompts with DIFFERENT tags.
        # prompt1 { prompt2 { shift2 k2. shift1 k1. body } ... }
        #
        # prompt1 {
        #   100 + prompt2 {
        #     20 + shift2 k2. shift1 k1. k1(k2(5))
        #   }
        # }
        # inner shift2 captures F2 = 20 + [] (within prompt2), its body
        # runs inside prompt2 and immediately performs shift1, which
        # captures through prompt2's boundary up to prompt1:
        #   F1 = 100 + prompt2 { [] }
        # k1(k2(5)): evaluate argument k2(5) first -> prompt2 { 20 + 5 } = 25
        # then k1(25) -> prompt1 { 100 + prompt2 { 25 } } = 125
        e = prompt(
            1,
            add(
                Int(100),
                prompt(
                    2,
                    add(
                        Int(20),
                        shift(
                            2,
                            "k2",
                            shift(
                                1,
                                "k1",
                                call(Var("k1"), call(Var("k2"), Int(5))),
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.assertEqual(evaluate(e).as_int(), 125)

    def test_inner_prompt_masks_outer_same_tag(self):
        # shift sees the NEAREST same-tag prompt.
        # prompt0 { 1 + prompt0 { 2 + shift0 k. 9 } }
        # inner shift captures only "2 + []" and returns 9; the result of
        # the inner prompt is 9; outer computes 1 + 9 = 10.
        e = prompt(
            0,
            add(
                Int(1),
                prompt(0, add(Int(2), shift(0, "k", Int(9)))),
            ),
        )
        self.assertEqual(evaluate(e).as_int(), 10)

    def test_distinct_tag_does_not_mask(self):
        # prompt1 { 100 + prompt2 { 20 + shift1 k. k(7) } }
        # the inner prompt2 does NOT bound tag1; k captures
        # 100 + prompt2 { [] } and resume yields 100 + prompt2 { 7 } = 107.
        e = prompt(
            1,
            add(
                Int(100),
                prompt(2, add(Int(20), shift(1, "k", call(Var("k"), Int(7))))),
            ),
        )
        self.assertEqual(evaluate(e).as_int(), 127)

    def test_no_matching_prompt_errors(self):
        # Required case: shift with no enclosing matching prompt.
        with self.assertRaises(NoPromptError) as cm:
            evaluate(shift(9, "k", Int(0)))
        self.assertEqual(cm.exception.code, "no-prompt")
        self.assertIn("9", str(cm.exception))

    def test_no_matching_prompt_wrong_tag_inside_prompt(self):
        with self.assertRaises(NoPromptError):
            evaluate(prompt(1, shift(2, "k", Int(0))))

    def test_resume_reinstalls_prompt_for_nested_shift(self):
        # The prompt reinstalled around a resumed continuation must catch
        # further same-tag shifts performed inside its argument.
        # prompt0 {
        #   1 + shift0 k. k( shift0 j. j(400) )
        # }
        # outer shift captures F = 1 + []; resuming k evaluates its
        # argument under a fresh prompt0; the inner shift0 is caught by
        # that prompt (otherwise it would be an unmatched-shift error),
        # captures the suspended k-call into j, and j(400) resumes into it:
        # k(400) = prompt0 { 1 + 400 } = 401.
        e = prompt(
            0,
            add(
                Int(1),
                shift(
                    0,
                    "k",
                    call(Var("k"), shift(0, "j", call(Var("j"), Int(400)))),
                ),
            ),
        )
        self.assertEqual(evaluate(e).as_int(), 401)

    def test_continuation_under_lambda_delimited(self):
        # prompt0 { (lam f. f(0)) (lam x. x + shift0 k. 7) }
        # shift escapes past BOTH the application and the call, bounded by
        # prompt0; result is 7.
        e = prompt(
            0,
            call(
                lam("f", call(Var("f"), Int(0))),
                lam("x", add(Var("x"), shift(0, "k", Int(7)))),
            ),
        )
        self.assertEqual(evaluate(e).as_int(), 7)


if __name__ == "__main__":
    unittest.main()
