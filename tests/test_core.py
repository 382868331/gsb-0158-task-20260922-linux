"""被测核心的语义测试（手写用例，覆盖题面点名的四类场景及契约边界）。"""

from __future__ import annotations

import math
import unittest

from delimcc import (
    Add,
    Call,
    ContinuationAlreadyResumedError,
    Event,
    Int,
    InvalidProgramError,
    Lam,
    Prompt,
    Shift1,
    RuntimeTypeError,
    Shift,
    StepLimitError,
    UnboundVariableError,
    UncaughtShiftError,
    Var,
    interpret,
)


def val(e, **kw):
    return interpret(e, **kw).value


def iv(e, **kw):
    return interpret(e, **kw).value.value


class TestBasics(unittest.TestCase):
    def test_int_add_left_to_right(self):
        self.assertEqual(iv(Add(Int(2), Int(3))), 5)
        # 事件顺序证明左到右：先加左 2 与右 3。
        ev = interpret(Add(Int(2), Int(3)), trace=True).events
        self.assertIn(Event("add", 2, 3, 5), ev)

    def test_nested_arithmetic_and_closures(self):
        # (lambda x. x + 10)((lambda y. y + 1)(5)) = 16
        e = Call(
            Lam("x", Add(Var("x"), Int(10))),
            Call(Lam("y", Add(Var("y"), Int(1))), Int(5)),
        )
        self.assertEqual(iv(e), 16)

    def test_lexical_scoping_shadowing(self):
        # (lambda x. (lambda x. x) (x + 1))(40) -> 41
        e = Call(
            Lam("x", Call(Lam("x", Var("x")), Add(Var("x"), Int(1)))),
            Int(40),
        )
        self.assertEqual(iv(e), 41)

    def test_unbound_variable(self):
        with self.assertRaises(UnboundVariableError):
            interpret(Var("missing"))


class TestPromptShift(unittest.TestCase):
    def test_no_shift_prompt_is_identity(self):
        self.assertEqual(iv(Prompt(1, Add(Int(1), Int(2)))), 3)

    def test_nonlocal_return(self):
        # prompt( (lambda x. x + 1)(shift(k, 42)) ) —— 上下文被放弃，
        # body 42 直接成为 prompt 结果：非局部返回。
        e = Prompt(
            1,
            Call(Lam("x", Add(Var("x"), Int(1))), Shift(1, "k", Int(42))),
        )
        self.assertEqual(iv(e), 42)

    def test_uncaught_shift_is_error(self):
        with self.assertRaises(UncaughtShiftError) as cm:
            interpret(Shift(5, "k", Int(0)))
        self.assertEqual(cm.exception.tag, 5)

    def test_uncaught_inside_lambda_still_errors(self):
        # 未被同 tag prompt 包围，即使包在 lambda 体内。
        e = Call(Lam("z", Shift(8, "k", Var("z"))), Int(3))
        with self.assertRaises(UncaughtShiftError):
            interpret(e)

    def test_nearest_prompt_wins_with_same_tag(self):
        # 外层/内层同 tag：shift 只剥到内层，body 11 直接成为内层结果；
        # 外层对其再 +100。
        e = Prompt(
            3,
            Add(
                Int(100),
                Prompt(3, Add(Int(1), Shift(3, "k", Int(11)))),
            ),
        )
        self.assertEqual(iv(e), 111)

    def test_nested_different_tags(self):
        # prompt1(prompt2(1 + shift2(k, shift1(j, j(k(3))))))
        # k 恢复 [1 + ·]，j 恢复到 prompt1（其内侧为整个 prompt2 上下文）。
        e = Prompt(
            1,
            Prompt(
                2,
                Add(
                    Int(1),
                    Shift(
                        2,
                        "k",
                        Shift(
                            1,
                            "j",
                            Call(Var("j"), Call(Var("k"), Int(3))),
                        ),
                    ),
                ),
            ),
        )
        self.assertEqual(iv(e), 4)

    def test_nested_different_tags_independent_resume(self):
        # prompt1( 10 + prompt2( shift2(k, shift1(j, j(k(200))) ) ) )
        # k 上下文仅到 prompt2（空），j 上下文为 10 + prompt2(·)。
        e = Prompt(
            1,
            Add(
                Int(10),
                Prompt(
                    2,
                    Shift(
                        2,
                        "k",
                        Shift(1, "j", Call(Var("j"), Call(Var("k"), Int(200)))),
                    ),
                ),
            ),
        )
        self.assertEqual(iv(e), 210)

    def test_shift_body_evaluates_inside_prompt(self):
        # body 内再发同 tag shift，应能看到保留的同一 prompt。
        e = Prompt(
            4,
            Shift(4, "k", Shift(4, "j", Int(7))),
        )
        self.assertEqual(iv(e), 7)


class TestContinuationReuse(unittest.TestCase):
    def test_repeated_k_nested_calls(self):
        # 10 + shift(k, k(k(5))) = 10 + (10 + 5) = 25
        e = Prompt(
            1,
            Add(
                Int(10),
                Shift(1, "k", Call(Var("k"), Call(Var("k"), Int(5)))),
            ),
        )
        self.assertEqual(iv(e), 25)

    def test_repeated_k_independent_calls(self):
        # shift(k, k(2) + k(3))，上下文为恒等（prompt 紧邻），= 5
        e = Prompt(
            3,
            Shift(
                3,
                "k",
                Add(Call(Var("k"), Int(2)), Call(Var("k"), Int(3))),
            ),
        )
        self.assertEqual(iv(e), 5)

    def test_repeated_k_three_times_with_arithmetic(self):
        # 100 + shift(k, k(k(k(0)))) = 300
        e = Prompt(
            7,
            Add(
                Int(100),
                Shift(
                    7,
                    "k",
                    Call(Var("k"), Call(Var("k"), Call(Var("k"), Int(0)))),
                ),
            ),
        )
        self.assertEqual(iv(e), 300)

    def test_capture_right_side_of_add(self):
        # 1 + prompt( shift(k, k(k(2))) )：捕获不超过 prompt，
        # 外层 1 + · 在边界之外，prompt 结果为 2，总和 3。
        e = Add(
            Int(1),
            Prompt(1, Shift(1, "k", Call(Var("k"), Call(Var("k"), Int(2))))),
        )
        self.assertEqual(iv(e), 3)

    def test_capture_add_right_frame_inside_prompt(self):
        # prompt( 1 + (2 + shift(k, k(k(10)))) )
        # 捕获帧含两层加法右侧：1 + (2 + ·)，k(k(10)) = 1+(2+(1+(2+10))) = 16
        e = Prompt(
            9,
            Add(
                Int(1),
                Add(
                    Int(2),
                    Shift(9, "k", Call(Var("k"), Call(Var("k"), Int(10)))),
                ),
            ),
        )
        self.assertEqual(iv(e), 16)

    def test_composable_resume_keeps_callers(self):
        # prompt1( (lambda a. a + 1)( shift1(k, k(k(0))) ) )
        # k 捕获 (lambda a. a+1)(·)：k(0)=1, k(1)=2 -> 2
        e = Prompt(
            1,
            Call(
                Lam("a", Add(Var("a"), Int(1))),
                Shift(1, "k", Call(Var("k"), Call(Var("k"), Int(0)))),
            ),
        )
        self.assertEqual(iv(e), 2)

    def test_k_is_first_class_value(self):
        # shift body 把 k 传给函数再调用：(lambda f. f(f(4)))(k)
        e = Prompt(
            2,
            Add(
                Int(100),
                Shift(
                    2,
                    "k",
                    Call(
                        Lam("f", Call(Var("f"), Call(Var("f"), Int(4)))),
                        Var("k"),
                    ),
                ),
            ),
        )
        self.assertEqual(iv(e), 204)

    def test_events_observe_capture_and_rewrap(self):
        e = Prompt(1, Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5)))))
        names = [ev.name for ev in interpret(e, trace=True).events]
        self.assertEqual(names[0], "prompt_enter")
        self.assertIn("shift_capture", names)
        self.assertIn("cont_call", names)
        self.assertIn("prompt_rewrap_enter", names)
        self.assertIn("prompt_rewrap_exit", names)
        self.assertEqual(names[-1], "result")


class TestOneShotContinuation(unittest.TestCase):
    def test_single_resume_works_like_shift(self):
        # 10 + shift1(k, k(5)) = 15（一次恢复与 shift 行为一致）。
        e = Prompt(
            1,
            Add(Int(10), Shift1(1, "k", Call(Var("k"), Int(5)))),
        )
        self.assertEqual(iv(e), 15)

    def test_second_resume_fails_even_nested(self):
        # k(k(5))：第二次恢复在恢复后上下文内发生，必须报错。
        e = Prompt(
            1,
            Add(Int(10), Shift1(1, "k", Call(Var("k"), Call(Var("k"), Int(5))))),
        )
        with self.assertRaises(ContinuationAlreadyResumedError) as cm:
            interpret(e)
        self.assertEqual(cm.exception.tag, 1)

    def test_second_resume_fails_independent_calls(self):
        # k(2) + k(3)：第一次调用成功后，加法右支的第二次调用失败。
        e = Prompt(
            3,
            Shift1(
                3,
                "k",
                Add(Call(Var("k"), Int(2)), Call(Var("k"), Int(3))),
            ),
        )
        with self.assertRaises(ContinuationAlreadyResumedError):
            interpret(e)

    def test_spent_state_shared_through_alias(self):
        # k 经别名 f 传给另一变量：f 恢复成功后，再用 k 必须失败；
        # 反之亦然（对象身份共享，不是按变量名记账）。
        e_alias_first = Prompt(
            2,
            Add(
                Int(100),
                Shift1(
                    2,
                    "k",
                    Call(
                        Lam(
                            "f",
                            Add(
                                Call(Var("f"), Int(4)),
                                Call(Var("k"), Int(5)),
                            ),
                        ),
                        Var("k"),
                    ),
                ),
            ),
        )
        with self.assertRaises(ContinuationAlreadyResumedError):
            interpret(e_alias_first)

    def test_failure_before_consume_does_not_spend(self):
        # 实参自身求值失败（类型错误）发生在消费点之前：续延保持
        # fresh，不能误报“一次性续延复用”。消费翻转的值级不变量见
        # TestOneShotValueState；语言内无异常处理构造，故这里整体以
        # 实参的 RuntimeTypeError 终止，而不是 ContinuationAlreadyResumed。
        e = Prompt(
            6,
            Add(
                Int(10),
                Shift1(
                    6,
                    "k",
                        Call(
                            Lam("first", Call(Var("k"), Int(7))),
                            Call(Var("k"), Add(Int(1), Lam("z", Var("z")))),
                        ),
                ),
            ),
        )
        with self.assertRaises(RuntimeTypeError):
            interpret(e)

    def test_one_shot_and_reusable_coexist_nested(self):
        # prompt1(prompt2( 1 + shift1_2(k, shift_1(j, j(k(3)) + j(100))) ))
        # j 是可复用续延（调用两次），k 是一次性续延（只调用一次）：
        # j 上下文为 prompt2(...) 整体（含结果），j(X) = X。
        # 内层：k(3)=4；j(4)+j(100)=4+100=104。
        e = Prompt(
            1,
            Prompt(
                2,
                Add(
                    Int(1),
                    Shift1(
                        2,
                        "k",
                        Shift(
                            1,
                            "j",
                            Add(
                                Call(Var("j"), Call(Var("k"), Int(3))),
                                Call(Var("j"), Int(100)),
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.assertEqual(iv(e), 104)

    def test_same_tag_nesting_scopes_oneshot_correctly(self):
        # 同 tag 嵌套：内层 shift1 只捕获到内层 prompt；外层 +100 保留。
        e = Prompt(
            3,
            Add(
                Int(100),
                Prompt(
                    3,
                    Add(Int(1), Shift1(3, "k", Call(Var("k"), Int(11)))),
                ),
            ),
        )
        self.assertEqual(iv(e), 112)

    def test_uncaught_shift1_is_same_semantic_error(self):
        with self.assertRaises(UncaughtShiftError) as cm:
            interpret(Shift1(5, "k", Int(0)))
        self.assertEqual(cm.exception.tag, 5)

    def test_nonlocal_return_abandons_oneshot(self):
        # body 不调用 k：一次性续延被放弃，42 直接成为 prompt 结果。
        e = Prompt(
            1,
            Call(Lam("x", Add(Var("x"), Int(1))), Shift1(1, "k", Int(42))),
        )
        self.assertEqual(iv(e), 42)

    def test_events_distinguish_shift1(self):
        e = Prompt(1, Add(Int(10), Shift1(1, "k", Call(Var("k"), Int(5)))))
        names = [ev.name for ev in interpret(e, trace=True).events]
        self.assertIn("shift1_capture", names)
        self.assertIn("oneshot_cont_call", names)
        self.assertIn("prompt_rewrap_enter", names)
        self.assertNotIn("shift_capture", names)

    def test_validation_shift1(self):
        with self.assertRaises(InvalidProgramError):
            Shift1(True, "k", Int(1))  # type: ignore[arg-type]
        with self.assertRaises(InvalidProgramError):
            Shift1(1, "", Int(1))
        with self.assertRaises(InvalidProgramError):
            Shift1(1, "k", 2)  # type: ignore[arg-type]


class TestOneShotValueState(unittest.TestCase):
    def test_consume_is_identity_shared_and_atomic(self):
        from delimcc import OneShotContVal

        k = OneShotContVal(9, ())
        alias = k  # 同一对象的另一变量名
        self.assertFalse(k.spent)
        self.assertIn("fresh", k.display())
        k.consume()  # 第一次：提交成功
        self.assertTrue(k.spent)
        self.assertIn("spent", alias.display())
        with self.assertRaises(ContinuationAlreadyResumedError):
            alias.consume()  # 通过别名的第二次恢复必须失败


class TestRuntimeTypeErrors(unittest.TestCase):
    def test_add_non_int_rhs(self):
        with self.assertRaises(RuntimeTypeError):
            interpret(Add(Int(1), Lam("x", Var("x"))))

    def test_add_non_int_lhs(self):
        with self.assertRaises(RuntimeTypeError):
            interpret(Add(Lam("x", Var("x")), Int(1)))

    def test_call_non_callable(self):
        with self.assertRaises(RuntimeTypeError):
            interpret(Call(Int(1), Int(2)))

    def test_add_with_captured_continuation_value(self):
        # 直接把续延值拿去做加法（不调用）：类型错误。
        e = Prompt(1, Shift(1, "k", Add(Int(1), Var("k"))))
        with self.assertRaises(RuntimeTypeError):
            interpret(e)


class TestInputValidation(unittest.TestCase):
    def test_int_rejects_bool(self):
        with self.assertRaises(InvalidProgramError):
            Int(True)  # type: ignore[arg-type]

    def test_int_rejects_float_nan_and_inf(self):
        for bad in (1.5, float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidProgramError):
                    Int(bad)  # type: ignore[arg-type]
        self.assertTrue(math.isnan(float("nan")))  # 明确覆盖 NaN 来源

    def test_tag_rejects_bool_float_str(self):
        body = Int(1)
        for bad in (True, 1.0, "1", None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidProgramError):
                    Prompt(bad, body)  # type: ignore[arg-type]
                with self.assertRaises(InvalidProgramError):
                    Shift(bad, "k", body)  # type: ignore[arg-type]

    def test_names_must_be_nonempty_str(self):
        with self.assertRaises(InvalidProgramError):
            Var("")
        with self.assertRaises(InvalidProgramError):
            Lam(3, Int(1))  # type: ignore[arg-type]
        with self.assertRaises(InvalidProgramError):
            Shift(1, "", Int(1))

    def test_children_must_be_expr(self):
        with self.assertRaises(InvalidProgramError):
            Add(Int(1), 2)  # type: ignore[arg-type]

    def test_interpret_rejects_non_expr_and_bad_limits(self):
        with self.assertRaises(InvalidProgramError):
            interpret(42)  # type: ignore[arg-type]
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidProgramError):
                    interpret(Int(1), max_nodes=bad)  # type: ignore[arg-type]
                with self.assertRaises(InvalidProgramError):
                    interpret(Int(1), max_steps=bad)  # type: ignore[arg-type]


class TestLimits(unittest.TestCase):
    def test_node_limit_is_pre_eval(self):
        # 每次循环增加 Add 与 Int 各一个节点；500 次 => 1001 个节点。
        # 即使语义平凡，也必须在求值前因超过 1000 的接口边界被拒绝。
        e = Int(0)
        for _ in range(500):
            e = Add(e, Int(0))
        self.assertEqual(e.node_count(), 1001)
        with self.assertRaises(InvalidProgramError):
            interpret(e)
        # 放宽到接口边界即可求值。
        self.assertEqual(iv(e, max_nodes=1001), 0)

    def test_step_limit_distinct_from_uncaught(self):
        # 预算耗尽（StepLimitError）与无解（UncaughtShiftError）必须分开：
        # 这里制造需要 >3 步的程序，给 3 步预算。
        e = Add(Int(1), Add(Int(2), Int(3)))
        with self.assertRaises(StepLimitError):
            interpret(e, max_steps=3)
        # 同程序给足预算正常返回。
        self.assertEqual(iv(e, max_steps=50), 6)

    def test_step_counter_reported(self):
        r = interpret(Add(Int(1), Int(2)))
        self.assertGreaterEqual(r.steps, 1)
        self.assertEqual(r.nodes, 3)


if __name__ == "__main__":
    unittest.main()
