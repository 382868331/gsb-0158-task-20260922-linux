"""Wind(before, body, after) 作用域钩子的语义测试。

期望值全部手算（事件序列在断言中逐字写出），另含与独立参考机的
**完整事件序列**差分（tests/reference.py，不导入核心）。两机的作用域
id 各自独立计数，比较前按出现顺序归一化。
"""

from __future__ import annotations

import re
import sys
import os
import unittest

import delimcc
from delimcc import (
    Add,
    Call,
    Int,
    InvalidProgramError,
    Lam,
    Prompt,
    RuntimeTypeError,
    Shift,
    UnboundVariableError,
    UncaughtShiftError,
    HookShiftError,
    Var,
    Wind,
    interpret,
)

sys.path.insert(0, os.path.dirname(__file__))

from program_gen import generate, to_ast  # noqa: E402
from reference import run_trace  # noqa: E402


def tuples(events):
    return [(ev.name,) + tuple(ev.args) for ev in events]


def wind_trace(expr):
    return tuples(interpret(expr, trace=True).events)


def wind_only(events):
    return [ev for ev in events if ev[0] in ("wind_before", "wind_after")]


_REIFIED_CONT = re.compile(r"^<lambda [A-Za-z0-9_]+__\d+>$")


def canon_value(s):
    """统一两机对“未调用续延值”的不透明展示。

    CEK 机保留为 ``<continuation tag=.. depth=..>``；替换机按定义把 k
    具体化为 ``<lambda x__N>``（fresh 名）。二者是同一不透明可调用值，
    事件顺序/值语义一致，仅展示文本不同，比对前统一为占位符。
    """
    if isinstance(s, str) and (s.startswith("<continuation") or _REIFIED_CONT.match(s)):
        return "<opaque-callable>"
    return s


def normalize(events):
    """归一化 wind 作用域 id（按首次出现）与不透明续延值展示。

    两机独立分配 id，顺序语义一致即可比；其余事件原样保留。
    """
    table: dict = {}
    out = []
    for ev in events:
        if ev[0] in ("wind_before", "wind_after"):
            sid = ev[1]
            if sid not in table:
                table[sid] = len(table)
            out.append((ev[0], table[sid], ev[2]))
        elif ev[0] in ("prompt_exit", "prompt_rewrap_exit", "result"):
            out.append(ev[:-1] + (canon_value(ev[-1]),))
        else:
            out.append(ev)
    return out


class TestWindNormal(unittest.TestCase):
    def test_value_is_body_hook_values_discarded(self):
        # before/after 即使算出不同整数也被丢弃，Wind 的值取 body。
        e = Wind(Int(11), Int(22), Int(33))
        self.assertEqual(interpret(e).value.value, 22)

    def test_plain_event_sequence(self):
        e = Wind(Int(11), Int(22), Int(33))
        self.assertEqual(
            wind_trace(e),
            [
                ("wind_before", 0, "enter"),
                ("wind_after", 0, "normal"),
                ("result", "22"),
            ],
        )

    def test_nested_entry_exit_order(self):
        # 进入自外向内，离开自内向外；值取最内 body=9。
        e = Wind(Int(1), Wind(Int(2), Int(9), Int(4)), Int(3))
        self.assertEqual(interpret(e).value.value, 9)
        self.assertEqual(
            wind_only(wind_trace(e)),
            [
                ("wind_before", 0, "enter"),
                ("wind_before", 1, "enter"),
                ("wind_after", 1, "normal"),
                ("wind_after", 0, "normal"),
            ],
        )

    def test_body_composes_with_arithmetic(self):
        # wind(1, 40, 2) + 5 = 45；钩子只产生事件，不影响 body 值。
        e = Add(Wind(Int(1), Int(40), Int(2)), Int(5))
        self.assertEqual(interpret(e).value.value, 45)

    def test_original_constructs_still_work_inside_wind(self):
        # lambda/调用在作用域内正常工作。
        e = Wind(Int(0), Call(Lam("x", Add(Var("x"), Int(1))), Int(41)), Int(0))
        self.assertEqual(interpret(e).value.value, 42)


class TestWindCaptureAndResume(unittest.TestCase):
    def test_capture_crossing_single_scope(self):
        # prompt( wind(b=100, 10 + shift(k, k(5)), a=200) )
        # 期望（手算）：进入 -> 捕获 -> capture-after；
        # 恢复 -> resume-before -> 加法 -> normal-after，值 15。
        e = Prompt(
            1,
            Wind(
                Int(100),
                Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5)))),
                Int(200),
            ),
        )
        self.assertEqual(interpret(e).value.value, 15)
        self.assertEqual(
            wind_only(wind_trace(e)),
            [
                ("wind_before", 0, "enter"),
                ("wind_after", 0, "capture"),
                ("wind_before", 0, "resume"),
                ("wind_after", 0, "normal"),
            ],
        )

    def test_full_event_sequence_single_scope(self):
        e = Prompt(
            1,
            Wind(
                Int(100),
                Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5)))),
                Int(200),
            ),
        )
        self.assertEqual(
            wind_trace(e),
            [
                ("prompt_enter", 1),
                ("wind_before", 0, "enter"),
                ("shift_capture", 1, 2),
                ("wind_after", 0, "capture"),
                ("cont_call", 1, 2),
                ("prompt_rewrap_enter", 1),
                ("wind_before", 0, "resume"),
                ("add", 10, 5, 15),
                ("wind_after", 0, "normal"),
                ("prompt_rewrap_exit", 1, "15"),
                ("prompt_exit", 1, "15"),
                ("result", "15"),
            ],
        )

    def test_nested_scopes_double_resume_order(self):
        # 两层 wind 包住 10 + shift(k, k(k(5)))；值 25。
        inner = Wind(
            Int(200),
            Add(Int(10), Shift(1, "k", Call(Var("k"), Call(Var("k"), Int(5))))),
            Int(201),
        )
        outer = Wind(Int(100), inner, Int(101))
        e = Prompt(1, outer)
        self.assertEqual(interpret(e).value.value, 25)
        self.assertEqual(
            wind_only(wind_trace(e)),
            [
                ("wind_before", 0, "enter"),
                ("wind_before", 1, "enter"),
                # 捕获：自内向外离开。
                ("wind_after", 1, "capture"),
                ("wind_after", 0, "capture"),
                # 第一次恢复：自外向内进入，再自内向外离开。
                ("wind_before", 0, "resume"),
                ("wind_before", 1, "resume"),
                ("wind_after", 1, "normal"),
                ("wind_after", 0, "normal"),
                # 第二次恢复：同样完整配对。
                ("wind_before", 0, "resume"),
                ("wind_before", 1, "resume"),
                ("wind_after", 1, "normal"),
                ("wind_after", 0, "normal"),
            ],
        )

    def test_scope_outside_prompt_not_captured(self):
        # wind 在 prompt 外：捕获区不跨越它，无 capture/resume，
        # 仅正常 enter/normal。
        e = Wind(Int(1), Prompt(1, Shift(1, "k", Int(42))), Int(2))
        self.assertEqual(interpret(e).value.value, 42)
        self.assertEqual(
            wind_only(wind_trace(e)),
            [
                ("wind_before", 0, "enter"),
                ("wind_after", 0, "normal"),
            ],
        )

    def test_nonlocal_return_leaves_scope_via_capture(self):
        # wind 在 prompt 内，shift body 不调用 k：作用域随捕获离开
        # （capture），之后不再 normal 退出，值 42。
        e = Prompt(1, Wind(Int(1), Add(Int(10), Shift(1, "k", Int(42))), Int(2)))
        self.assertEqual(interpret(e).value.value, 42)
        self.assertEqual(
            wind_only(wind_trace(e)),
            [
                ("wind_before", 0, "enter"),
                ("wind_after", 0, "capture"),
            ],
        )

    def test_scope_already_exited_not_recaptured(self):
        # wind 作为加法左值，在 shift 求值前已正常退出；捕获不再涉及它。
        e = Prompt(1, Add(Wind(Int(1), Int(5), Int(2)), Shift(1, "k", Int(7))))
        self.assertEqual(interpret(e).value.value, 7)
        self.assertEqual(
            wind_only(wind_trace(e)),
            [
                ("wind_before", 0, "enter"),
                ("wind_after", 0, "normal"),
            ],
        )

    def test_repeated_resume_uses_same_scope_id(self):
        # 同一作用域多次恢复，事件 id 相同、why 为 resume。
        e = Prompt(
            1,
            Wind(
                Int(0),
                Add(Int(1), Shift(1, "k", Call(Var("k"), Call(Var("k"), Int(0))))),
                Int(0),
            ),
        )
        interpret(e, trace=True)
        ids = {
            ev[1]
            for ev in wind_trace(e)
            if ev[0] == "wind_before" and ev[2] == "resume"
        }
        self.assertEqual(ids, {0})

    def test_resume_rebuilds_inner_prompt_interleaved_with_wind(self):
        # prompt2( wind(100, prompt1( shift2(k, k(k(3))) ), 200) )
        # 每次 k 恢复都重放：rewrap prompt2 -> wind resume-before ->
        # 重建被一起捕获的 prompt1（enter/exit）-> wind normal-after。
        s = (
            "prompt",
            2,
            (
                "wind",
                ("int", 100),
                (
                    "prompt",
                    1,
                    ("shift", 2, "k",
                     ("call", ("var", "k"), ("call", ("var", "k"), ("int", 3)))),
                ),
                ("int", 200),
            ),
        )
        e = to_ast(s)
        self.assertEqual(interpret(e).value.value, 3)
        self.assertEqual(
            wind_trace(e),
            [
                ("prompt_enter", 2),
                ("wind_before", 0, "enter"),
                ("prompt_enter", 1),
                ("shift_capture", 2, 2),
                ("wind_after", 0, "capture"),
                ("cont_call", 2, 2),
                ("prompt_rewrap_enter", 2),
                ("wind_before", 0, "resume"),
                ("prompt_enter", 1),
                ("prompt_exit", 1, "3"),
                ("wind_after", 0, "normal"),
                ("prompt_rewrap_exit", 2, "3"),
                ("cont_call", 2, 2),
                ("prompt_rewrap_enter", 2),
                ("wind_before", 0, "resume"),
                ("prompt_enter", 1),
                ("prompt_exit", 1, "3"),
                ("wind_after", 0, "normal"),
                ("prompt_rewrap_exit", 2, "3"),
                ("prompt_exit", 2, "3"),
                ("result", "3"),
            ],
        )


class TestWindErrors(unittest.TestCase):
    def _wind_events(self, expr):
        try:
            interpret(expr, trace=True)
        except delimcc.EvaluationError as ex:
            return type(ex), wind_only(tuples(getattr(ex, "events", ())))
        raise AssertionError("应当抛出错误")

    def test_type_error_unwinds_inner_to_outer(self):
        e = Wind(
            Int(1),
            Wind(Int(2), Add(Int(1), Lam("z", Var("z"))), Int(4)),
            Int(3),
        )
        kind, ev = self._wind_events(e)
        self.assertIs(kind, RuntimeTypeError)
        self.assertEqual(
            ev,
            [
                ("wind_before", 0, "enter"),
                ("wind_before", 1, "enter"),
                ("wind_after", 1, "error"),
                ("wind_after", 0, "error"),
            ],
        )

    def test_uncaught_shift_runs_error_hooks(self):
        kind, ev = self._wind_events(Wind(Int(1), Shift(9, "k", Int(0)), Int(2)))
        self.assertIs(kind, UncaughtShiftError)
        self.assertEqual(
            ev,
            [
                ("wind_before", 0, "enter"),
                ("wind_after", 0, "error"),
            ],
        )

    def test_before_failure_does_not_enter_scope(self):
        # before 引用未定义变量：作用域未进入，after 不执行。
        kind, ev = self._wind_events(Wind(Var("missing"), Int(1), Int(2)))
        self.assertIs(kind, UnboundVariableError)
        self.assertEqual(ev, [("wind_before", 0, "enter")])

    def test_capture_after_failure_aborts_continuation(self):
        # 捕获区内 wind 的 after 本身失败：记 capture（已开始离开）后
        # 抛出错误，续延不生成，shift body 不执行。
        bad = Wind(Int(1), Shift(1, "k", Int(0)), Var("gone"))
        kind, ev = self._wind_events(Prompt(1, bad))
        self.assertIs(kind, UnboundVariableError)
        self.assertEqual(
            ev,
            [
                ("wind_before", 0, "enter"),
                ("wind_after", 0, "capture"),
            ],
        )

    def test_capture_after_failure_outer_scopes_unwind_as_error(self):
        # 内层 capture-after 失败；更外层被带走的作用域改按 error 拆卸。
        inner = Wind(Int(2), Shift(1, "k", Int(0)), Var("gone"))
        outer = Wind(Int(1), inner, Int(11))
        kind, ev = self._wind_events(Prompt(1, outer))
        self.assertIs(kind, UnboundVariableError)
        self.assertEqual(
            ev,
            [
                ("wind_before", 0, "enter"),
                ("wind_before", 1, "enter"),
                ("wind_after", 1, "capture"),
                ("wind_after", 0, "error"),
            ],
        )

    def test_capture_teardown_failure_propagates_beyond_prompt(self):
        # prompt 外还有一层 wind：拆卸失败后错误继续穿过它。
        mid = Wind(Int(2), Shift(1, "k", Int(0)), Var("gone"))
        kind, ev = self._wind_events(Wind(Int(1), Prompt(1, mid), Int(11)))
        self.assertIs(kind, UnboundVariableError)
        self.assertEqual(
            ev,
            [
                ("wind_before", 0, "enter"),
                ("wind_before", 1, "enter"),
                ("wind_after", 1, "capture"),
                ("wind_after", 0, "error"),
            ],
        )

    def test_partial_resume_failure_only_entered_scopes_unwind(self):
        # 外层 before 正常进入成功、内层 before 表达式本身非法（未定义
        # 变量）：内层未进入不跑钩子，只有已进入的外层 error-after。
        inner = Wind(Var("gone"), Add(Int(1), Shift(1, "k", Call(Var("k"), Int(5)))), Int(9))
        outer = Wind(Int(1), inner, Int(8))
        kind, ev = self._wind_events(Prompt(1, outer))
        self.assertIs(kind, UnboundVariableError)
        self.assertEqual(
            ev,
            [
                ("wind_before", 0, "enter"),
                ("wind_before", 1, "enter"),
                ("wind_after", 0, "error"),
            ],
        )

    def test_error_hook_itself_failing_replaces_error(self):
        # body 类型错误；内层 after 也坏：内层 error-after 触发后抛新错误，
        # 外层 error-after 仍执行（携带新错误）。
        inner = Wind(Int(2), Add(Int(1), Lam("z", Var("z"))), Var("gone"))
        outer = Wind(Int(1), inner, Int(8))
        kind, ev = self._wind_events(outer)
        self.assertIs(kind, UnboundVariableError)
        self.assertEqual(
            ev,
            [
                ("wind_before", 0, "enter"),
                ("wind_before", 1, "enter"),
                ("wind_after", 1, "error"),
                ("wind_after", 0, "error"),
            ],
        )

    def test_trace_false_error_has_empty_events(self):
        e = Wind(Int(1), Add(Int(1), Lam("z", Var("z"))), Int(2))
        with self.assertRaises(RuntimeTypeError) as cm:
            interpret(e, trace=False)
        self.assertEqual(cm.exception.events, ())


class TestWindStaticValidation(unittest.TestCase):
    def test_direct_shift_in_before_rejected(self):
        with self.assertRaises(InvalidProgramError):
            Wind(Shift(1, "k", Int(0)), Int(1), Int(2))

    def test_direct_shift_in_after_rejected(self):
        with self.assertRaises(InvalidProgramError):
            Wind(Int(1), Int(1), Shift(1, "k", Int(0)))

    def test_shift_nested_in_lambda_inside_hook_rejected(self):
        with self.assertRaises(InvalidProgramError):
            Wind(Int(1), Int(1), Call(Lam("z", Shift(1, "k", Var("z"))), Int(0)))

    def test_shift_in_body_is_allowed(self):
        # body 中出现 shift 是合法的（只有钩子被禁止）。
        e = Prompt(1, Wind(Int(1), Shift(1, "k", Int(5)), Int(2)))
        self.assertEqual(interpret(e).value.value, 5)

    def test_hook_children_must_be_expr(self):
        with self.assertRaises(InvalidProgramError):
            Wind(1, Int(1), Int(2))  # type: ignore[arg-type]
        with self.assertRaises(InvalidProgramError):
            Wind(Int(1), 1, Int(2))  # type: ignore[arg-type]

    def test_constructor_is_atomic_no_partial_state(self):
        # 非法钩子时 Wind 节点根本不产生；已构造的合法子节点不受影响。
        before = Int(1)
        with self.assertRaises(InvalidProgramError):
            Wind(Shift(1, "k", Int(0)), before, Int(2))
        self.assertEqual(before.value, 1)


class TestWindDynamicHookShift(unittest.TestCase):
    def test_shift_via_closure_called_in_hook_is_error(self):
        # 钩子 AST 静态无 shift，但动态调用了词法环境中含 shift 的闭包：
        # 运行期 HookShiftError（钩子不捕获续延）。
        prog = Prompt(
            1,
            Call(
                Lam("g", Wind(Call(Var("g"), Int(0)), Int(1), Int(2))),
                Lam("u", Shift(1, "k", Int(3))),
            ),
        )
        with self.assertRaises(HookShiftError) as cm:
            interpret(prog)
        self.assertEqual(cm.exception.tag, 1)


class TestWindCrossCheck(unittest.TestCase):
    """成功路径上与独立参考机比较**完整事件序列**（归一化作用域 id）。"""

    HANDS = {
        "plain": ("wind", ("int", 11), ("int", 22), ("int", 33)),
        "nested": (
            "wind",
            ("int", 1),
            ("wind", ("int", 2), ("int", 9), ("int", 4)),
            ("int", 3),
        ),
        "capture_one": (
            "prompt",
            1,
            (
                "wind",
                ("int", 100),
                ("add", ("int", 10), ("shift", 1, "k", ("call", ("var", "k"), ("int", 5)))),
                ("int", 200),
            ),
        ),
        "nested_double": (
            "prompt",
            1,
            (
                "wind",
                ("int", 100),
                (
                    "wind",
                    ("int", 200),
                    (
                        "add",
                        ("int", 10),
                        (
                            "shift",
                            1,
                            "k",
                            ("call", ("var", "k"), ("call", ("var", "k"), ("int", 5))),
                        ),
                    ),
                    ("int", 201),
                ),
                ("int", 101),
            ),
        ),
        "nonlocal": (
            "prompt",
            1,
            ("wind", ("int", 1), ("add", ("int", 10), ("shift", 1, "k", ("int", 42))), ("int", 2)),
        ),
        "outside": (
            "wind",
            ("int", 1),
            ("prompt", 1, ("shift", 1, "k", ("int", 42))),
            ("int", 2),
        ),
    }

    def test_handwritten_event_trails_match_reference(self):
        for name, schema in self.HANDS.items():
            with self.subTest(case=name):
                ref_term, ref_events = run_trace(schema)
                self.assertEqual(ref_term[0], "int")
                core = wind_trace(to_ast(schema))
                self.assertEqual(
                    normalize(core),
                    normalize(ref_events),
                    msg=f"case={name}",
                )

    def test_generated_event_trails_match_reference(self):
        # 对含 wind 的成功程序比较完整事件轨迹；其余成功程序比较值。
        n_wind_compared = 0
        n_value_compared = 0
        for seed in range(120):
            schema = generate(seed, max_depth=4)
            try:
                ref_term, ref_events = run_trace(schema)
            except Exception:
                continue  # 参考机报错的程序由既有差分测试覆盖
            core_res = interpret(to_ast(schema), trace=True)
            if ref_term[0] != "int":
                continue
            self.assertEqual(core_res.value.display(), str(ref_term[1]), msg=f"seed={seed}")
            core_events = tuples(core_res.events)
            contains_wind = any(x[0] in ("wind_before", "wind_after") for x in core_events)
            if contains_wind:
                self.assertEqual(
                    normalize(core_events),
                    normalize(ref_events),
                    msg=f"seed={seed} wind 事件序列分歧",
                )
                n_wind_compared += 1
            else:
                n_value_compared += 1
        # 保证确实比对到了相当数量的 wind 程序，而非空跑。
        self.assertGreater(n_wind_compared, 20)


if __name__ == "__main__":
    unittest.main()
