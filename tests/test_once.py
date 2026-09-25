"""一次性续延（ShiftOnce/shift1）语义测试。

覆盖题面点名的四类场景，并用手写的小步事件序列核对消费时点：
  1. 别名双调用——句柄经变量/参数转手，任意别名的第二次进入都被拒绝；
  2. 参数求值失败——句柄尚未消费（含实参中 shift 放弃整个调用的情形）；
  3. 第一次进入后失败/预算耗尽——消费不回滚（由错误事件轨迹证明时点）；
  4. 一次性续延体内再捕获普通续延——普通续延仍可多次调用。

所有期望值另由独立参考机 tests/reference.py（不导入核心）交叉判定；
错误位置断言只使用 (文件, 行, 列) 基本元组，不序列化宿主函数、
不执行任何用户 Python 文本。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from delimcc import (  # noqa: E402
    Add,
    Call,
    ConsumedContinuationError,
    Event,
    Int,
    InvalidProgramError,
    Lam,
    OnceContVal,
    Prompt,
    Shift,
    ShiftOnce,
    StepLimitError,
    RuntimeTypeError,
    UncaughtShiftError,
    Var,
    interpret,
)
from program_gen import to_ast  # noqa: E402
from reference import RefError, run  # noqa: E402

LOC_F = ("prog.e", 10, 5)
LOC_C = ("prog.e", 10, 17)
LOC_A1 = ("prog.e", 11, 3)
LOC_A2 = ("prog.e", 12, 3)


def names(err_or_result):
    return [ev.name for ev in err_or_result.events]


class TestAliasDoubleCall(unittest.TestCase):
    def test_direct_k_k_rejected(self):
        # prompt1(10 + shift1(k, k(k(5))))：第一次进入恢复 10+·，
        # 第二次必须报告已消费；参考机独立给出同样结论。
        schema = (
            "prompt", 1,
            ("add", ("int", 10),
             ("shift1", 1, "k",
              ("call", ("var", "k"), ("call", ("var", "k"), ("int", 5))))),
        )
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "consumed")

        prog = to_ast(schema)
        with self.assertRaises(ConsumedContinuationError) as cm:
            interpret(prog, trace=True)
        ex = cm.exception
        self.assertEqual(ex.tag, 1)
        # 原子检查点事件：先一次 fresh（本次消费），再一次 consumed（拒绝）。
        once = [ev for ev in ex.events if ev.name == "once_cont_call"]
        self.assertEqual(
            [(ev.args[2]) for ev in once], ["fresh", "consumed"]
        )
        # 截获的上下文只重包/执行一次：第二次在拼接任何帧之前被拒。
        self.assertEqual(names(ex).count("prompt_rewrap_enter"), 1)

    def test_handle_through_lambda_alias(self):
        # (lambda a. a(5) + a(6))(k)：别名 a 与 k 共享同一消费单元。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(
                    1, "k",
                    Call(
                        Lam("a", Add(Call(Var("a"), Int(5)), Call(Var("a"), Int(6)))),
                        Var("k"),
                    ),
                ),
            ),
        )
        with self.assertRaises(ConsumedContinuationError) as cm:
            interpret(prog, trace=True)
        self.assertEqual(
            [ev.args[2] for ev in cm.exception.events if ev.name == "once_cont_call"],
            ["fresh", "consumed"],
        )

    def test_two_independent_aliases_share_cell(self):
        # (lambda a. (lambda b. a(5) + b(6))(k))(k)
        prog = Prompt(
            1,
            ShiftOnce(
                1, "k",
                Call(
                    Lam("a",
                        Call(Lam("b", Add(Call(Var("a"), Int(5)),
                                          Call(Var("b"), Int(6)))),
                             Var("k"))),
                    Var("k"),
                ),
            ),
        )
        with self.assertRaises(ConsumedContinuationError):
            interpret(prog)

    def test_deep_alias_chain_still_single_shot(self):
        # 句柄穿过三层 lambda 别名 a -> p -> q，q 第一次进入成功，
        # 另一别名 p 第二次进入被拒。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(
                    1, "k",
                    Call(
                        Lam(
                            "a",
                            Call(
                                Lam(
                                    "p",
                                    Call(
                                        Lam("q",
                                            Add(Call(Var("q"), Int(1)),
                                                Call(Var("p"), Int(2)))),
                                        Var("p"),
                                    ),
                                ),
                                Var("a"),
                            ),
                        ),
                        Var("k"),
                    ),
                ),
            ),
        )
        with self.assertRaises(ConsumedContinuationError):
            interpret(prog)

    def test_consumed_handle_escapes_prompt_then_rejected_outside(self):
        # k(0) 在 prompt 内消费句柄；句柄本身作为 prompt 结果逃出定界，
        # 在没有任何同 tag prompt 的外层经别名 g 再次调用——仍被拒绝，
        # 因为消费单元挂在句柄值上，不依赖定界帧是否还在栈上。
        escaped = Prompt(
            1,
            ShiftOnce(
                1, "k",
                Call(Lam("x", Var("k")), Call(Var("k"), Int(0))),
            ),
        )
        outer = Call(Lam("g", Add(Call(Var("g"), Int(7)), Int(100))), escaped)
        with self.assertRaises(ConsumedContinuationError) as cm:
            interpret(outer, trace=True)
        self.assertEqual(
            [ev.args[2] for ev in cm.exception.events if ev.name == "once_cont_call"],
            ["fresh", "consumed"],
        )

    def test_locations_pinpoint_three_sites(self):
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(
                    1, "k",
                    Add(
                        Call(Var("k"), Int(5)).with_loc(LOC_A1),
                        Call(Var("k"), Int(6)).with_loc(LOC_A2),
                    ),
                ).with_loc(LOC_C),
            ),
        ).with_loc(LOC_F)
        with self.assertRaises(ConsumedContinuationError) as cm:
            interpret(prog)
        ex = cm.exception
        self.assertEqual(ex.capture_loc, LOC_C)
        self.assertEqual(ex.first_loc, LOC_A1)
        self.assertEqual(ex.second_loc, LOC_A2)
        self.assertIn("prog.e:10:17", str(ex))
        self.assertIn("prog.e:11:3", str(ex))
        self.assertIn("prog.e:12:3", str(ex))

    def test_unused_handle_leaks_as_fresh_value(self):
        # 不调用 k 而是把句柄作为结果返回：仍是 fresh 的不透明值。
        prog = Prompt(1, ShiftOnce(1, "k", Var("k")))
        v = interpret(prog).value
        self.assertIsInstance(v, OnceContVal)
        self.assertFalse(v.consumed)
        self.assertEqual(v.display(), "<once-continuation tag=1 depth=0 fresh>")


class TestArgumentFailureNotConsumed(unittest.TestCase):
    def test_arg_type_error_precedes_consume(self):
        # k(1 + (lambda z.z))：实参求值即类型错误，句柄未消费。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(
                    1, "k",
                    Call(Var("k"), Add(Int(1), Lam("z", Var("z")))),
                ),
            ),
        )
        with self.assertRaises(RuntimeTypeError) as cm:
            interpret(prog, trace=True)
        # 轨迹在 shift1_capture 后没有任何 once_cont_call：检查点未到达。
        self.assertNotIn("once_cont_call", names(cm.exception))

    def test_uncaught_shift_in_arg_precedes_consume(self):
        # 实参里有未匹配的 shift：先报 UncaughtShiftError，句柄未消费。
        prog = Prompt(
            1,
            ShiftOnce(
                1, "k",
                Call(Var("k"), Add(Int(1), Shift(9, "e", Int(0)))),
            ),
        )
        with self.assertRaises(UncaughtShiftError) as cm:
            interpret(prog, trace=True)
        self.assertNotIn("once_cont_call", names(cm.exception))

    def test_arg_shift_abandons_call_handle_still_fresh(self):
        # 实参在更内层同 tag prompt 中 shift 放弃整个 k(·) 调用，
        # 控制以 99 成为该 prompt 的值；k 从未被消费，之后调用一次
        # 成功：99 + (10 + 5) = 114。这是“参数失败未消费”的效果版
        # （控制流被效果重排，但消费检查点同样未到达）。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(
                    1, "k",
                    Add(
                        Prompt(
                            1,
                            Call(Var("k"), Add(Int(1), Shift(1, "e", Int(99)))),
                        ),
                        Call(Var("k"), Int(5)),
                    ),
                ),
            ),
        )
        r = interpret(prog, trace=True)
        self.assertEqual(r.value.value, 114)
        # 恰好一次到达检查点，且标记为 fresh。
        self.assertEqual(
            [ev.args[2] for ev in r.events if ev.name == "once_cont_call"],
            ["fresh"],
        )
        schema = (
            "prompt", 1,
            ("add", ("int", 10),
             ("shift1", 1, "k",
              ("add",
               ("prompt", 1,
                ("call", ("var", "k"),
                 ("add", ("int", 1), ("shift", 1, "e", ("int", 99))))),
               ("call", ("var", "k"), ("int", 5))))),
        )
        ref = run(schema)
        self.assertEqual(ref, ("int", 114))


    def test_bad_arg_on_retry_still_reports_type_error(self):
        # 第一次 k(5) 已消费；第二次调用的实参本身类型错误。按值左到右：
        # 实参先于消费检查点求值，故第二次以 RuntimeTypeError 失败而非
        # ConsumedContinuationError——但轨迹证明第一次消费已经发生。
        prog = Prompt(
            1,
            ShiftOnce(
                1, "k",
                Add(
                    Call(Var("k"), Int(5)),
                    Call(Var("k"), Add(Int(1), Lam("z", Var("z")))),
                ),
            ),
        )
        with self.assertRaises(RuntimeTypeError) as cm:
            interpret(prog, trace=True)
        self.assertEqual(
            [ev.args[2] for ev in cm.exception.events if ev.name == "once_cont_call"],
            ["fresh"],
        )


class TestFailureAfterEntry(unittest.TestCase):
    def test_error_after_entry_shows_consume_first(self):
        # k((lambda z.z))：进入后恢复 10+·，捕获的加法帧要求整数，
        # 触发 RuntimeTypeError。事件轨迹证明消费发生在出错之前。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(1, "k", Call(Var("k"), Lam("z", Var("z")))),
            ),
        )
        with self.assertRaises(RuntimeTypeError) as cm:
            interpret(prog, trace=True)
        evs = cm.exception.events
        self.assertIn(Event("once_cont_call", 1, 1, "fresh", None), evs)
        # 消费之后才有重包进入；没有第二次 once_cont_call 的机会，
        # 但消费状态不因错误回滚（fresh 事件之后无任何“复位”事件）。
        seq = [ev.name for ev in evs]
        self.assertLess(seq.index("once_cont_call"), seq.index("prompt_rewrap_enter"))
        self.assertEqual(seq.count("once_cont_call"), 1)

    def test_step_budget_exhausted_after_entry_not_revived(self):
        # 给极小步数预算，使句柄进入（fresh）后在恢复途中预算耗尽。
        # StepLimitError 是资源错误；轨迹显示消费先于预算耗尽，没有
        # 任何路径让已进入的句柄复活。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(1, "k", Call(Var("k"), Int(5))),
            ),
        )
        with self.assertRaises(StepLimitError) as cm:
            interpret(prog, trace=True, max_steps=12)
        seq = names(cm.exception)
        self.assertIn("once_cont_call", seq)
        self.assertIn("prompt_rewrap_enter", seq)
        # 预算耗尽是最后事实；其后不存在更多机器迁移。
        once = [ev for ev in cm.exception.events if ev.name == "once_cont_call"]
        self.assertEqual(once[0].args[2], "fresh")

    def test_budget_failure_before_checkpoint_does_not_consume(self):
        # 对照：预算在实参求值阶段耗尽时，检查点尚未到达。
        prog = Prompt(
            1,
            ShiftOnce(1, "k", Call(Var("k"), Add(Int(1), Int(2)))),
        )
        with self.assertRaises(StepLimitError) as cm:
            interpret(prog, trace=True, max_steps=4)
        self.assertNotIn("once_cont_call", names(cm.exception))

    def test_effect_aborted_first_resume_not_revived(self):
        # 更强的“已进入不复活”证明：第一次进入后恢复途中触发另一 tag
        # 的 shift，以非局部返回放弃整个重包（不是错误、预算继续），
        # shift1 body 继续执行第二次调用——句柄仍必须已消费。
        schema = (
            "prompt", 2,
            ("prompt", 1,
             ("call", ("lam", "r", ("shift", 2, "h", ("var", "r"))),
              ("add", ("int", 100),
               ("shift1", 1, "k",
                ("add",
                 ("prompt", 2, ("call", ("var", "k"), ("int", 0))),
                 ("call", ("var", "k"), ("int", 6))))))),
        )
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "consumed")

        with self.assertRaises(ConsumedContinuationError) as cm:
            interpret(to_ast(schema), trace=True)
        statuses = [
            ev.args[2] for ev in cm.exception.events if ev.name == "once_cont_call"
        ]
        self.assertEqual(statuses, ["fresh", "consumed"])


class TestMixedWithNormalShift(unittest.TestCase):
    def test_normal_continuation_inside_once_body(self):
        # shift1(k, shift(j, j(k(5)) + j(2)))：j 捕获到外层 prompt 的
        # 空上下文（addR(10) 帧已被 shift1 先行截走），故 j 是恒等；
        # k 唯一一次进入恢复 10+5=15，j(15)=15、j(2)=2，总和 17。
        # 普通续延 j 调用两次，一次性续延 k 只进入一次。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(
                    1, "k",
                    Shift(
                        1, "j",
                        Add(
                            Call(Var("j"), Call(Var("k"), Int(5))),
                            Call(Var("j"), Int(2)),
                        ),
                    ),
                ),
            ),
        )
        r = interpret(prog, trace=True)
        self.assertEqual(r.value.value, 17)
        self.assertEqual(
            [ev.args[2] for ev in r.events if ev.name == "once_cont_call"],
            ["fresh"],
        )
        # 普通续延 j 两次调用，事件上有两次 cont_call。
        self.assertEqual(
            [ev.name for ev in r.events].count("cont_call"), 2
        )
        schema = (
            "prompt", 1,
            ("add", ("int", 10),
             ("shift1", 1, "k",
              ("shift", 1, "j",
               ("add", ("call", ("var", "j"), ("call", ("var", "k"), ("int", 5))),
                      ("call", ("var", "j"), ("int", 2)))))),
        )
        self.assertEqual(run(schema), ("int", 17))

    def test_nested_different_tags_once_and_multi(self):
        # prompt1(prompt2(1 + shift1_2(k, shift_1(j, j(k(3))))))
        # k 一次性（上下文 1+·），j 普通（可重入）；参考机期望值 4。
        schema = (
            "prompt", 1,
            ("prompt", 2,
             ("add", ("int", 1),
              ("shift1", 2, "k",
               ("shift", 1, "j",
                ("call", ("var", "j"), ("call", ("var", "k"), ("int", 3))))))),
        )
        self.assertEqual(run(schema), ("int", 4))
        self.assertEqual(interpret(to_ast(schema)).value.value, 4)

    def test_normal_shift_still_multi_call(self):
        # 同 tag 下 Shift（多次）与 ShiftOnce（一次）并存，原语义不被收紧。
        prog = Prompt(
            1,
            Add(
                Int(10),
                Shift(
                    1, "m",
                    Add(
                        Call(Var("m"), Call(Var("m"), Int(1))),
                        Prompt(
                            1,
                            ShiftOnce(1, "k", Call(Var("k"), Int(2))),
                        ),
                    ),
                ),
            ),
        )
        # m 上下文 10+·：m(m(1)) = 10+(10+1) = 21；内层 prompt=2；21+2=23。
        self.assertEqual(interpret(prog).value.value, 23)

    def test_small_step_event_sequence_hand_checked(self):
        # 对“实参中 shift 放弃调用、随后唯一一次进入”的程序逐事件核对。
        prog = Prompt(
            1,
            Add(
                Int(10),
                ShiftOnce(
                    1, "k",
                    Add(
                        Prompt(
                            1,
                            Call(Var("k"), Add(Int(1), Shift(1, "e", Int(99)))),
                        ),
                        Call(Var("k"), Int(5)),
                    ),
                ),
            ),
        )
        events = interpret(prog, trace=True).events
        expected = [
            ("prompt_enter", (1,)),
            ("shift1_capture", (1, 1, None)),
            ("prompt_enter", (1,)),
            ("shift_capture", (1, 2)),
            ("prompt_exit", (1, "99")),
            ("once_cont_call", (1, 1, "fresh", None)),
            ("prompt_rewrap_enter", (1,)),
            ("add", (10, 5, 15)),
            ("prompt_rewrap_exit", (1, "15")),
            ("add", (99, 15, 114)),
            ("prompt_exit", (1, "114")),
            ("result", ("114",)),
        ]
        self.assertEqual(
            [(ev.name, tuple(ev.args)) for ev in events], expected
        )


class TestOnceValidationAndErrors(unittest.TestCase):
    def test_shift1_rejects_bad_tag_name_child(self):
        with self.assertRaises(InvalidProgramError):
            ShiftOnce(True, "k", Int(1))  # type: ignore[arg-type]
        with self.assertRaises(InvalidProgramError):
            ShiftOnce(1, "", Int(1))
        with self.assertRaises(InvalidProgramError):
            ShiftOnce(1, "k", 2)  # type: ignore[arg-type]

    def test_shift1_uncaught_uses_nearest_prompt_rule(self):
        with self.assertRaises(UncaughtShiftError) as cm:
            interpret(ShiftOnce(6, "k", Int(0)))
        self.assertEqual(cm.exception.tag, 6)
        with self.assertRaises(RefError) as rcm:
            run(("shift1", 6, "k", ("int", 0)))
        self.assertEqual(rcm.exception.kind, "uncaught")

    def test_once_handle_not_callable_as_int(self):
        # 句柄参与加法：类型错误，且两侧实现一致。
        schema = ("prompt", 1, ("shift1", 1, "k", ("add", ("int", 1), ("var", "k"))))
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "type")
        with self.assertRaises(RuntimeTypeError):
            interpret(to_ast(schema))


if __name__ == "__main__":
    unittest.main()
