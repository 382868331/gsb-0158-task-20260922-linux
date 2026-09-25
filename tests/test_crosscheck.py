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
    "consumed": delimcc.ConsumedContinuationError,
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
            else "consumed"
            if isinstance(ex, delimcc.ConsumedContinuationError)
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
        "once_single_ok": (
            "prompt",
            1,
            (
                "add",
                ("int", 10),
                ("shift1", 1, "k", ("call", ("var", "k"), ("int", 5))),
            ),
        ),
        "once_nested_tags": (
            "prompt",
            1,
            (
                "prompt",
                2,
                (
                    "add",
                    ("int", 1),
                    (
                        "shift1",
                        2,
                        "k",
                        (
                            "shift1",
                            1,
                            "j",
                            ("call", ("var", "j"), ("call", ("var", "k"), ("int", 3))),
                        ),
                    ),
                ),
            ),
        ),
        "once_alias_then_normal": (
            "prompt",
            1,
            (
                "add",
                ("int", 10),
                (
                    "shift1",
                    1,
                    "k",
                    (
                        "shift",
                        1,
                        "j",
                        (
                            "add",
                            ("call", ("var", "j"), ("call", ("var", "k"), ("int", 5))),
                            ("call", ("var", "j"), ("int", 2)),
                        ),
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

    def test_once_double_consumption_both_error(self):
        # 期望值（'consumed' 类别）只由独立参考机产生。
        schema = (
            "prompt",
            1,
            (
                "add",
                ("int", 10),
                (
                    "shift1",
                    1,
                    "k",
                    ("call", ("lam", "a",
                              ("add", ("call", ("var", "a"), ("int", 5)),
                                     ("call", ("var", "a"), ("int", 6)))),
                     ("var", "k")),
                ),
            ),
        )
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "consumed")
        with self.assertRaises(delimcc.ConsumedContinuationError):
            delimcc.interpret(to_ast(schema))

    def test_once_arg_failure_both_error(self):
        # 实参类型错误：两侧都报 type，而非 consumed（检查点未到达）。
        schema = (
            "prompt",
            1,
            (
                "shift1",
                1,
                "k",
                ("call", ("var", "k"), ("add", ("int", 1), ("lam", "z", ("var", "z")))),
            ),
        )
        with self.assertRaises(RefError) as cm:
            run(schema)
        self.assertEqual(cm.exception.kind, "type")
        with self.assertRaises(delimcc.RuntimeTypeError):
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


class TestGeneratedCrossCheck(unittest.TestCase):
    """确定性随机程序的双机等价（值相同，或错误类别相同）。"""

    def test_many_generated_programs(self):
        n_checked = 0
        n_int_matches = 0
        n_err_matches = 0
        for seed in range(120):
            schema = generate(seed, max_depth=4)
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
        # 保证测试不是“零覆盖”：两类结果都应实际出现。
        self.assertGreater(n_int_matches, 30)
        self.assertGreater(n_err_matches, 5)


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
