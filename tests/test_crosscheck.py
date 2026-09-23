"""交叉测试：独立参考机（小步替换）判定期望值，对照被测核心。

参考机不导入核心；期望值只由参考机得出。往返一致（同程序多次求值
结果相同）只作为补充断言，见 TestIdempotence。
"""

from __future__ import annotations

import unittest

import delimcc

from program_gen import generate, to_ast
from reference import RefError, run

# 参考机错误类别 -> 核心应抛出的错误类型
ERR_MAP = {
    "uncaught": delimcc.UncaughtShiftError,
    "hook": delimcc.HookViolationError,
    "type": delimcc.RuntimeTypeError,
    "unbound": delimcc.UnboundVariableError,
}


def run_both(schema):
    """返回 ('ok', 整数值) 或 ('err', 类别)。两侧分别独立执行。"""
    ref_out = None
    try:
        ref = run(schema)
    except RefError as ex:
        ref_out = ("err", ex.kind)
    else:
        ref_out = ("ok", ref)

    try:
        got = delimcc.interpret(to_ast(schema)).value
    except delimcc.EvaluationError as ex:
        got_kind = (
            "uncaught"
            if isinstance(ex, delimcc.UncaughtShiftError)
            else "hook"
            if isinstance(ex, delimcc.HookViolationError)
            else "type"
            if isinstance(ex, delimcc.RuntimeTypeError)
            else "unbound"
            if isinstance(ex, delimcc.UnboundVariableError)
            else "steps"
            if isinstance(ex, delimcc.StepLimitError)
            else "other"
        )
        core_out = ("err", got_kind)
    else:
        core_out = ("ok", got)

    return ref_out, core_out


class TestHandWrittenAgainstReference(unittest.TestCase):
    """题面点名场景：每个都由参考机独立给出期望。"""

    CASES = {
        "nested_diff_tags": (
            "prompt",
            1,
            (
                "prompt",
                2,
                (
                    "add",
                    ("int", 1),
                    (
                        "shift",
                        2,
                        "k",
                        (
                            "shift",
                            1,
                            "j",
                            ("call", ("var", "j"), ("call", ("var", "k"), ("int", 3))),
                        ),
                    ),
                ),
            ),
        ),
        "repeated_k": (
            "prompt",
            1,
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
        ),
        "capture_add_right": (
            "add",
            ("int", 1),
            (
                "prompt",
                1,
                ("shift", 1, "k", ("call", ("var", "k"), ("call", ("var", "k"), ("int", 2)))),
            ),
        ),
        "capture_deep_add_frames": (
            "prompt",
            9,
            (
                "add",
                ("int", 1),
                (
                    "add",
                    ("int", 2),
                    ("shift", 9, "k", ("call", ("var", "k"), ("call", ("var", "k"), ("int", 10)))),
                ),
            ),
        ),
        "nonlocal_return": (
            "prompt",
            1,
            (
                "call",
                ("lam", "x", ("add", ("var", "x"), ("int", 1))),
                ("shift", 1, "k", ("int", 42)),
            ),
        ),
        "k_first_class": (
            "prompt",
            2,
            (
                "add",
                ("int", 100),
                (
                    "shift",
                    2,
                    "k",
                    (
                        "call",
                        ("lam", "f", ("call", ("var", "f"), ("call", ("var", "f"), ("int", 4)))),
                        ("var", "k"),
                    ),
                ),
            ),
        ),
    }

    def test_handwritten_cases_match_reference(self):
        for name, schema in self.CASES.items():
            with self.subTest(case=name):
                ref = run(schema)
                self.assertEqual(ref[0], "int", f"参考机应得到整数: {ref!r}")
                got = delimcc.interpret(to_ast(schema)).value
                self.assertEqual(got.display(), str(ref[1]))

    def test_unmatched_prompt_both_error(self):
        schema = ("shift", 9, "k", ("int", 1))
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "uncaught")
        with self.assertRaises(delimcc.UncaughtShiftError):
            delimcc.interpret(to_ast(schema))

    def test_type_error_both_error(self):
        schema = ("add", ("int", 1), ("lam", "x", ("var", "x")))
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "type")
        with self.assertRaises(delimcc.RuntimeTypeError):
            delimcc.interpret(to_ast(schema))

    def test_unbound_both_error(self):
        schema = ("var", "ghost")
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "unbound")
        with self.assertRaises(delimcc.UnboundVariableError):
            delimcc.interpret(to_ast(schema))


class TestWindAgainstReference(unittest.TestCase):
    """wind 的值/错误类别与 wind 钩子事件顺序，都由独立参考机给期望。"""

    WIND_CASES = {
        # 正常嵌套 LIFO
        "nested": (
            "wind", ("int", 10),
            ("wind", ("int", 20), ("int", 30), ("int", 21)),
            ("int", 11),
        ),
        # 单层作用域被捕获再恢复
        "capture_resume": (
            "prompt", 1,
            ("wind", ("int", 2),
             ("add", ("int", 100),
              ("shift", 1, "k", ("call", ("var", "k"), ("int", 5)))),
             ("int", 3)),
        ),
        # 两个作用域都被捕获：离开内->外，重入外->内，再离开内->外
        "two_scopes": (
            "prompt", 1,
            ("wind", ("int", 11),
             ("wind", ("int", 22),
              ("add", ("int", 100),
               ("shift", 1, "k", ("call", ("var", "k"), ("int", 5)))),
              ("int", 33)),
             ("int", 44)),
        ),
        # 非局部返回：离开后不重入
        "nonlocal": (
            "prompt", 1,
            ("wind", ("int", 2),
             ("add", ("int", 100), ("shift", 1, "k", ("int", 42))),
             ("int", 3)),
        ),
        # 作用域在捕获 prompt 之外，恢复不重入
        "outside": (
            "wind", ("int", 1),
            ("prompt", 1,
             ("add", ("int", 100),
              ("shift", 1, "k", ("call", ("var", "k"), ("int", 5))))),
            ("int", 2),
        ),
        # k 的调用点在新 wind 内：该 wind 不被捕获
        "call_site_scope": (
            "prompt", 1,
            ("add", ("int", 1),
             ("shift", 1, "k",
              ("wind", ("int", 8), ("call", ("var", "k"), ("int", 7)), ("int", 9)))),
        ),
        # body 类型错误：展开跑 after 后重抛
        "body_error": (
            "wind", ("int", 1),
            ("add", ("int", 1), ("lam", "x", ("var", "x"))),
            ("int", 99),
        ),
        # before 自身失败：不进入、不跑 after
        "before_error": (
            "wind", ("add", ("int", 1), ("lam", "x", ("var", "x"))),
            ("int", 2), ("int", 3),
        ),
        # 钩子里 shift / 调续延：钩子违约
        "shift_in_before": ("prompt", 1, ("wind", ("shift", 1, "k", ("int", 0)),
                                          ("int", 2), ("int", 3))),
        "call_k_in_before": (
            "prompt", 1,
            ("shift", 1, "k",
             ("wind", ("call", ("var", "k"), ("int", 3)), ("int", 1), ("int", 2))),
        ),
    }

    def _run_core_events(self, schema):
        try:
            r = delimcc.interpret(to_ast(schema), trace=True)
            wind = [(e.name,) + tuple(e.args)
                    for e in r.events if e.name in ("wind_before", "wind_after")]
            disp = r.value.display()
            return ("ok", disp, wind)
        except delimcc.EvaluationError as ex:
            wind = [(e.name,) + tuple(e.args)
                    for e in getattr(ex, "events", ())
                    if e.name in ("wind_before", "wind_after")]
            kind = (
                "uncaught" if isinstance(ex, delimcc.UncaughtShiftError)
                else "hook" if isinstance(ex, delimcc.HookViolationError)
                else "type" if isinstance(ex, delimcc.RuntimeTypeError)
                else "unbound" if isinstance(ex, delimcc.UnboundVariableError)
                else "steps" if isinstance(ex, delimcc.StepLimitError)
                else "other"
            )
            return ("err", kind, wind)

    def _run_ref_events(self, schema):
        evs = []
        try:
            t = run(schema, events=evs)
            disp = (str(t[1]) if t[0] == "int"
                    else f"<lambda {t[1]}>" if t[0] == "lam"
                    else f"<continuation tag={t[1]}>")
            return ("ok", disp, [tuple(x) for x in evs])
        except RefError as ex:
            return ("err", ex.kind, [tuple(x) for x in evs])

    def test_value_error_and_wind_events_match_reference(self):
        for name, schema in self.WIND_CASES.items():
            with self.subTest(case=name):
                core = self._run_core_events(schema)
                ref = self._run_ref_events(schema)
                self.assertEqual(core[0], ref[0], msg=name)
                self.assertEqual(core[1], ref[1], msg=name)
                # 关键：wind 进入/离开事件序列两机逐事件一致（含钩子值）。
                self.assertEqual(core[2], ref[2], msg=name)
                # 进出严格配对（每次 before 都有对应 after）。
                self.assertEqual(
                    [x[0] for x in core[2]].count("wind_before"),
                    [x[0] for x in core[2]].count("wind_after"),
                    msg=name,
                )

    def test_generated_wind_event_sequences_match(self):
        # 生成程序中含 wind 的子集：除值/错误类别外再逐事件比对 wind 序列。
        n_wind = 0
        for seed in range(200):
            schema = generate(seed, max_depth=4)
            if "wind" not in str(schema):
                continue
            n_wind += 1
            core = self._run_core_events(schema)
            ref = self._run_ref_events(schema)
            self.assertEqual(core[0], ref[0], msg=f"seed={seed}")
            self.assertEqual(core[1], ref[1], msg=f"seed={seed}")
            self.assertEqual(core[2], ref[2], msg=f"seed={seed}")
        self.assertGreater(n_wind, 30)


class TestGeneratedCrossCheck(unittest.TestCase):
    """确定性随机程序的双机等价（值相同，或错误类别相同）。"""

    N_SEEDS = 200

    def test_many_generated_programs(self):
        n_checked = 0
        n_int_matches = 0
        n_err_matches = 0
        n_wind = 0
        for seed in range(self.N_SEEDS):
            schema = generate(seed, max_depth=4)
            if "wind" in str(schema):
                n_wind += 1
            ref_out, core_out = run_both(schema)
            self.assertEqual(
                ref_out[0],
                core_out[0],
                msg=f"seed={seed} 成功/失败分歧: ref={ref_out} core={core_out}",
            )
            if ref_out[0] == "err":
                # 参考机不报 steps（它预算更大）；只对照语义错误类别。
                self.assertNotEqual(ref_out[1], "steps")
                self.assertEqual(
                    ref_out[1],
                    core_out[1],
                    msg=f"seed={seed} 错误类别分歧: {ref_out} vs {core_out}",
                )
                n_err_matches += 1
            else:
                ref_term = ref_out[1]
                if ref_term[0] == "int":
                    self.assertEqual(core_out[1].display(), str(ref_term[1]))
                    n_int_matches += 1
            n_checked += 1
        # 保证测试不是“零覆盖”：两类结果与 wind 程序都应实际出现。
        self.assertEqual(n_checked, self.N_SEEDS)
        self.assertGreater(n_int_matches, 50)
        self.assertGreater(n_err_matches, 5)
        self.assertGreater(n_wind, 30)


class TestReferenceIsIndependent(unittest.TestCase):
    def test_reference_does_not_import_core(self):
        import inspect

        import reference

        src = inspect.getsource(reference)
        self.assertNotIn("import delimcc", src)
        self.assertNotIn("from delimcc", src)


class TestIdempotence(unittest.TestCase):
    """补充（非判定手段）：同一不可变 AST 多次求值结果稳定。"""

    def test_repeat_evaluation_stable(self):
        schema = generate(777, max_depth=3)
        ast = to_ast(schema)
        first = delimcc.interpret(ast, trace=True)
        second = delimcc.interpret(ast, trace=True)
        self.assertEqual(first.value.display(), second.value.display())
        self.assertEqual(
            [tuple(e.args) for e in first.events],
            [tuple(e.args) for e in second.events],
        )
        # 续延值多次调用彼此不影响：k(k(5)) 重复求值一致。
        e = to_ast(
            (
                "prompt",
                1,
                ("add", ("int", 10), ("shift", 1, "k", ("call", ("var", "k"), ("call", ("var", "k"), ("int", 5))))),
            )
        )
        self.assertEqual(delimcc.interpret(e).value.display(), "25")
        self.assertEqual(delimcc.interpret(e).value.display(), "25")


if __name__ == "__main__":
    unittest.main()
