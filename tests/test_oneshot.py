"""一次性（one-shot）定界续延：ShiftOnce 的语义、消费时点与错误位置测试。

覆盖题面点名的场景：
  * 别名双调用——续延经多个变量别名转手也只能进入一次；
  * 参数求值失败——实参求值被中断时句柄尚未消费；
  * 第一次进入后失败——进入之后被中断/出错，句柄仍然已消费；
  * 一次性续延内捕获普通续延——普通（多次）续延不被偷加限制；
  * 预算耗尽——StepLimitError 之前已消费的句柄不复活。

消费时点用手写的小步事件序列（Event 列表）逐步核对；消费来源位置
（Call 节点的 loc）同时出现在 once_consume 事件与
ContinuationConsumedError 上。
"""

from __future__ import annotations

import unittest

from delimcc import (
    Add,
    Call,
    ContinuationConsumedError,
    Event,
    Int,
    InvalidProgramError,
    Lam,
    OnceContVal,
    Prompt,
    Shift,
    ShiftOnce,
    StepLimitError,
    UncaughtShiftError,
    Var,
    interpret,
)

# 调用位置（loc 三元组），用于核对“消费来源位置”。
LOC_A = ("prog", 3, 5)
LOC_B = ("prog", 3, 9)
LOC_REAL = ("prog", 7, 1)


def iv(e, **kw):
    return interpret(e, **kw).value.value


class TestOnceBasic(unittest.TestCase):
    def test_single_call_behaves_like_shift(self):
        # prompt(1, 10 + shift_once(k, k(5))) = 15，与多次版同形同值。
        e = Prompt(1, Add(Int(10), ShiftOnce(1, "k", Call(Var("k"), Int(5)))))
        self.assertEqual(iv(e), 15)

    def test_handwritten_small_step_sequence(self):
        # 逐步核对：实参先求值（add 事件），随后才是原子消费点
        # （once_consume），再进入重包 prompt。
        e = Prompt(
            1,
            ShiftOnce(1, "k", Call(Var("k"), Add(Int(1), Int(2)), LOC_REAL)),
        )
        r = interpret(e, trace=True)
        self.assertEqual(r.value.display(), "3")
        self.assertEqual(
            list(r.events),
            [
                Event("prompt_enter", 1),
                Event("once_capture", 1, 0),
                Event("add", 1, 2, 3),            # 实参求值完毕
                Event("once_consume", 1, 0, LOC_REAL),  # 原子检查并消费
                Event("prompt_rewrap_enter", 1),  # 之后才进入截获上下文
                Event("prompt_rewrap_exit", 1, "3"),
                Event("prompt_exit", 1, "3"),
                Event("result", "3"),
            ],
        )

    def test_uncaught_shift_once_is_error(self):
        with self.assertRaises(UncaughtShiftError) as cm:
            interpret(ShiftOnce(5, "k", Int(0)))
        self.assertEqual(cm.exception.tag, 5)

    def test_nearest_prompt_wins_same_tag(self):
        # 最近同 tag prompt 规则与 Shift 一致：shift_once 只剥到内层。
        e = Prompt(
            3,
            Add(Int(100), Prompt(3, Add(Int(1), ShiftOnce(3, "k", Int(11))))),
        )
        self.assertEqual(iv(e), 111)

    def test_shift_once_validation(self):
        with self.assertRaises(InvalidProgramError):
            ShiftOnce(True, "k", Int(1))  # type: ignore[arg-type]
        with self.assertRaises(InvalidProgramError):
            ShiftOnce(1.0, "k", Int(1))  # type: ignore[arg-type]
        with self.assertRaises(InvalidProgramError):
            ShiftOnce(1, "", Int(1))
        with self.assertRaises(InvalidProgramError):
            ShiftOnce(1, "k", 42)  # type: ignore[arg-type]


class TestAliasDoubleCall(unittest.TestCase):
    """别名双调用：k -> f -> g 两次转手，第二次进入报告已消费。"""

    PROG = Prompt(
        1,
        ShiftOnce(
            1,
            "k",
            Call(
                Lam(
                    "f",
                    Call(
                        Lam(
                            "g",
                            Add(
                                Call(Var("g"), Int(1), LOC_A),  # 首次消费点
                                Call(Var("g"), Int(2), LOC_B),  # 第二次进入
                            ),
                        ),
                        Var("f"),
                    ),
                ),
                Var("k"),
            ),
        ),
    )

    def test_second_call_reports_consumed_with_locations(self):
        with self.assertRaises(ContinuationConsumedError) as cm:
            interpret(self.PROG)
        exc = cm.exception
        self.assertEqual(exc.tag, 1)
        # 结果中给出消费来源位置：首次消费在 LOC_A，本次被拒在 LOC_B。
        self.assertEqual(exc.consumed_loc, LOC_A)
        self.assertEqual(exc.attempt_loc, LOC_B)
        msg = str(exc)
        self.assertIn("3:5", msg)
        self.assertIn("3:9", msg)

    def test_first_call_completed_before_second_failed(self):
        # trace=True：失败异常携带部分轨迹；once_consume 恰好一次且
        # 位置是 LOC_A——第一次进入已成功消费，第二次在消费点被拒。
        with self.assertRaises(ContinuationConsumedError) as cm:
            interpret(self.PROG, trace=True)
        consumes = [ev for ev in cm.exception.events if ev.name == "once_consume"]
        self.assertEqual(consumes, [Event("once_consume", 1, 0, LOC_A)])

    def test_multi_shot_shift_same_shape_unrestricted(self):
        # 对照：同形程序用 Shift（多次续延），g(1)+g(2) = 3，不加限制。
        e = Prompt(
            1,
            Shift(
                1,
                "k",
                Call(
                    Lam(
                        "f",
                        Call(
                            Lam(
                                "g",
                                Add(Call(Var("g"), Int(1)), Call(Var("g"), Int(2))),
                            ),
                            Var("f"),
                        ),
                    ),
                    Var("k"),
                ),
            ),
        )
        self.assertEqual(iv(e), 3)


class TestArgumentFailureNotConsumed(unittest.TestCase):
    """参数求值失败（被异 tag shift 中断）时句柄尚未消费。"""

    # prompt1( shift_once1(k, prompt2( k(shift2(j, k(2+3))) + 100 )) )
    # 第一次调用 k(...) 的实参求值被 shift2 整体中断（捕获并放弃了
    # callArg(k) 与 addL(100) 两帧），句柄不得被消费；随后 shift2 的
    # body 里真正的 k(2+3) 成功消费一次，结果为 5。
    PROG = Prompt(
        1,
        ShiftOnce(
            1,
            "k",
            Prompt(
                2,
                Add(
                    Call(
                        Var("k"),
                        Shift(2, "j", Call(Var("k"), Add(Int(2), Int(3)), LOC_REAL)),
                        LOC_A,
                    ),
                    Int(100),
                ),
            ),
        ),
    )

    def test_value(self):
        self.assertEqual(iv(self.PROG), 5)

    def test_handwritten_small_step_sequence(self):
        r = interpret(self.PROG, trace=True)
        self.assertEqual(
            list(r.events),
            [
                Event("prompt_enter", 1),
                Event("once_capture", 1, 0),
                Event("prompt_enter", 2),
                # 第一次调用的实参求值被 shift2 中断：捕获 callArg/addL 两帧，
                # 全程没有 once_consume —— 句柄尚未消费。
                Event("shift_capture", 2, 2),
                # shift2 body 中真正的调用：实参 2+3 先求值……
                Event("add", 2, 3, 5),
                # ……随后才原子消费（消费来源位置 LOC_REAL）。
                Event("once_consume", 1, 0, LOC_REAL),
                Event("prompt_rewrap_enter", 1),
                Event("prompt_rewrap_exit", 1, "5"),
                Event("prompt_exit", 2, "5"),
                Event("prompt_exit", 1, "5"),
                Event("result", "5"),
            ],
        )
        consumes = [ev for ev in r.events if ev.name == "once_consume"]
        self.assertEqual(len(consumes), 1)


class TestFailureAfterEntryStaysConsumed(unittest.TestCase):
    """第一次进入之后被中断：句柄仍然已消费，再次进入报告消费来源。"""

    # prompt1( shift_once1(k, prompt2( k@A(0) + k@B(1) )) + shift2(j, j(0)) )
    # k 的截获上下文是 [· + shift2(j, j(0))]：k@A(0) 消费后进入，求值到
    # shift2 时整体中断逃向 prompt2（第一次进入未完成）；j 的截获帧里
    # 保存着带 k 绑定的 addL 帧，j(0) 驱动它再次求值 k@B(1) —— 此时
    # 句柄已消费，必须报 ContinuationConsumedError(consumed=A, attempt=B)。
    PROG = Prompt(
        1,
        Add(
            ShiftOnce(
                1,
                "k",
                Prompt(
                    2,
                    Add(
                        Call(Var("k"), Int(0), LOC_A),
                        Call(Var("k"), Int(1), LOC_B),
                    ),
                ),
            ),
            Shift(2, "j", Call(Var("j"), Int(0))),
        ),
    )

    def test_consumed_error_reports_first_entry_location(self):
        with self.assertRaises(ContinuationConsumedError) as cm:
            interpret(self.PROG)
        exc = cm.exception
        self.assertEqual(exc.consumed_loc, LOC_A)
        self.assertEqual(exc.attempt_loc, LOC_B)

    def test_consume_happened_before_abort(self):
        with self.assertRaises(ContinuationConsumedError) as cm:
            interpret(self.PROG, trace=True)
        events = cm.exception.events
        consumes = [ev for ev in events if ev.name == "once_consume"]
        # 消费恰好一次（LOC_A）；之后的 shift_capture 是进入后的中断，
        # 没有第二个 once_consume，也没有任何“复活”事件。
        self.assertEqual(consumes, [Event("once_consume", 1, 1, LOC_A)])
        names = [ev.name for ev in events]
        self.assertLess(names.index("once_consume"), names.index("shift_capture"))


class TestOrdinaryContinuationInsideOnce(unittest.TestCase):
    """一次性续延的 body 内捕获普通（多次）续延：不对它偷加限制。"""

    # prompt1( shift_once1(k, prompt2(shift2(j, j(10)+j(20))) + k(5)) )
    # j 是普通续延，调用两次都合法；k 仍只消费一次。= 30 + 5 = 35
    PROG = Prompt(
        1,
        ShiftOnce(
            1,
            "k",
            Add(
                Prompt(
                    2,
                    Shift(
                        2,
                        "j",
                        Add(Call(Var("j"), Int(10)), Call(Var("j"), Int(20))),
                    ),
                ),
                Call(Var("k"), Int(5), LOC_REAL),
            ),
        ),
    )

    def test_value(self):
        self.assertEqual(iv(self.PROG), 35)

    def test_multi_shot_called_twice_once_shot_consumed_once(self):
        r = interpret(self.PROG, trace=True)
        cont_calls = [ev for ev in r.events if ev.name == "cont_call"]
        consumes = [ev for ev in r.events if ev.name == "once_consume"]
        self.assertEqual(len(cont_calls), 2)  # 普通续延 j 进入两次，无限制
        self.assertEqual(consumes, [Event("once_consume", 1, 0, LOC_REAL)])


class TestBudgetExhaustionDoesNotRevive(unittest.TestCase):
    """预算耗尽也不能让已经进入的句柄复活。"""

    # prompt1( shift_once1(k, k@A(0)) + BIG )：k 消费后进入截获上下文，
    # 其剩余计算 BIG 超出步数预算。
    BIG = Int(0)
    for _ in range(200):
        BIG = Add(BIG, Int(1))
    PROG = Prompt(
        1,
        Add(ShiftOnce(1, "k", Call(Var("k"), Int(0), LOC_A)), BIG),
    )

    def test_baseline_completes_with_budget(self):
        self.assertEqual(iv(self.PROG, max_steps=100000), 200)

    def test_step_limit_after_consume(self):
        with self.assertRaises(StepLimitError) as cm:
            interpret(self.PROG, trace=True, max_steps=100)
        consumes = [ev for ev in cm.exception.events if ev.name == "once_consume"]
        # 预算死亡之前句柄已被消费（恰好一次）；解释器没有回滚路径，
        # 异常直接外抛，不存在“复活”后重放截获上下文的事件。
        self.assertEqual(consumes, [Event("once_consume", 1, 1, LOC_A)])

    def test_consumption_state_does_not_leak_across_evaluations(self):
        # 消费状态属于单次求值中的句柄对象：同一 AST 重新求值是新句柄。
        with self.assertRaises(StepLimitError):
            interpret(self.PROG, max_steps=100)
        self.assertEqual(iv(self.PROG, max_steps=100000), 200)


class TestOnceHandleUnit(unittest.TestCase):
    """句柄对象层面：consume 是原子的检查并消费，且无重置接口。"""

    def test_consume_is_atomic_and_monotonic(self):
        h = OnceContVal(1, ())
        self.assertFalse(h.consumed)
        h.consume(LOC_A)
        self.assertTrue(h.consumed)
        self.assertEqual(h.consumed_loc, LOC_A)
        with self.assertRaises(ContinuationConsumedError) as cm:
            h.consume(LOC_B)
        self.assertEqual(cm.exception.consumed_loc, LOC_A)
        self.assertEqual(cm.exception.attempt_loc, LOC_B)
        # 失败的第二次 consume 不覆盖首次消费位置。
        self.assertEqual(h.consumed_loc, LOC_A)

    def test_handle_has_no_reset_api(self):
        h = OnceContVal(1, ())
        h.consume(LOC_A)
        self.assertFalse(any(name.startswith("reset") for name in dir(h)))
        with self.assertRaises(AttributeError):
            h.consumed = False  # type: ignore[misc]  # 只读属性，不可回写

    def test_display_reflects_state(self):
        h = OnceContVal(2, ())
        self.assertIn("未消费", h.display())
        h.consume(None)
        self.assertIn("已消费", h.display())


if __name__ == "__main__":
    unittest.main()
