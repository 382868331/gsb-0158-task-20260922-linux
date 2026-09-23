"""确定性测试程序生成器（测试基础设施，不是判定器）。

生成“模式（schema）”元组，再分别编译到：
  * 参考机直接消费的元组项（见 reference.py）；
  * 被测核心的 AST 节点。

生成器使用固定种子的线性同余 PRNG，与环境/时间无关，结果可复现。
语法受限以保证生成程序必然终止且规模有界：
  * 调用的函数位置只允许 lambda 或被 shift 绑定的续延变量 k；
  * k 的调用点是语法中有限出现，每次恢复只展开有界快照；
  * 递归深度参数 d 单调下降。
"""

from __future__ import annotations

from typing import List, Tuple

import delimcc

# 模式语法与 reference.py 的元组语法一致。
Schema = tuple


class Lcg:
    """固定种子线性同余生成器（数值配方来自 Numerical Recipes）。"""

    def __init__(self, seed: int = 20260922):
        self.state = seed & 0xFFFFFFFF

    def next(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state

    def below(self, n: int) -> int:
        return self.next() % n


TAGS = (1, 2, 7)


def generate(seed: int, max_depth: int = 4) -> Schema:
    """生成一个闭合程序（所有 shift 都有同 tag prompt 包围）。"""
    rng = Lcg(seed)

    def hook_expr(d: int, vars_: List[str]) -> Schema:
        """钩子表达式：只做事件记录，不含 shift/k 调用（契约）。

        用小整数、变量（词法环境里的普通变量）、lambda 与加法构造；
        变量若是续延 k，被调用/参与加法会与核心一样报 hook 之外的错误，
        故这里只允许取不以 k 开头的普通变量。
        """
        plain = [x for x in vars_ if not x.startswith("k")]
        roll = rng.below(3)
        if roll == 0 and plain:
            return ("var", plain[rng.below(len(plain))])
        if roll == 1 and d >= 1 and plain:
            x = f"h{len(vars_)}_d{d}"
            return (
                "add",
                ("int", rng.below(5)),
                ("call", ("lam", x, ("add", ("var", x), ("int", rng.below(3)))),
                      ("int", rng.below(5))),
            )
        return ("int", rng.below(100))

    def gen(d: int, vars_: List[str], tags: Tuple[int, ...]) -> Schema:
        if d == 0:
            if vars_ and rng.below(3) == 0:
                return ("var", vars_[rng.below(len(vars_))])
            return ("int", rng.below(6))
        choice = rng.below(8)
        if choice == 0:  # 加法
            return ("add", gen(d - 1, vars_, tags), gen(d - 1, vars_, tags))
        if choice == 1:  # 立即调用 lambda（fn 位置必为 lam）
            x = f"x{len(vars_)}_d{d}"
            return (
                "call",
                ("lam", x, gen(d - 1, vars_ + [x], tags)),
                gen(d - 1, vars_, tags),
            )
        if choice == 2:  # 一个值：变量、整数或 lambda（lambda 若随后被
            # 放入加法/被当整数调用即产生类型错误，属自然混合）
            roll = rng.below(3)
            if roll == 0 and vars_:
                return ("var", vars_[rng.below(len(vars_))])
            if roll == 1:
                x = f"v{len(vars_)}_d{d}"
                return ("lam", x, gen(d - 1, vars_ + [x], tags))
            return ("int", rng.below(6))
        if choice == 3 and tags:  # 嵌套异 tag 的 shift
            # 约 1/8 概率使用未被任何 prompt 包裹的 tag 99 -> uncaught。
            tag = 99 if rng.below(8) == 0 else tags[rng.below(len(tags))]
            k = f"k{tag}_{d}_{rng.below(1000)}"
            body = gen(d - 1, vars_ + [k], tags)
            # 一半概率在 body 中真正调用 k（实参为更小规模表达式）
            if rng.below(2) == 0:
                arg = gen(max(d - 2, 0), vars_, tags)
                body = ("call", ("var", k), arg)
                if rng.below(2) == 0:  # 再嵌套一次 k 调用：k(k(arg))
                    body = ("call", ("var", k), body)
            return ("shift", tag, k, body)
        if choice == 4:  # k 作为一等值参与（可造成类型错误，两侧机都应报错）
            kvars = [v for v in vars_ if v.startswith("k")]
            if kvars:
                return ("add", ("int", rng.below(4)), ("var", kvars[-1]))
            return ("int", rng.below(6))
        if choice == 5:  # wind 动态作用域（钩子为合规事件表达式）
            return (
                "wind",
                hook_expr(max(d - 2, 0), vars_),
                gen(d - 1, vars_, tags),
                hook_expr(max(d - 2, 0), vars_),
            )
        if choice in (6, 7) or not tags:  # 建立 prompt
            tag = TAGS[rng.below(len(TAGS))]
            return ("prompt", tag, gen(d - 1, vars_, tuple(dict.fromkeys(tags + (tag,)))))
        # 不可达
        return ("int", 0)  # pragma: no cover

    # 顶层包一层全部 tag 的 prompt，保证任何 shift 均有界；
    # 嵌套异 tag 由生成器内部的 prompt/shift 选择覆盖。
    body = gen(max_depth, [], ())
    for tag in TAGS:
        body = ("prompt", tag, body)
    return body


def to_ast(s: Schema):
    """把模式编译成被测核心的 AST。"""
    t = s[0]
    if t == "int":
        return delimcc.Int(s[1])
    if t == "var":
        return delimcc.Var(s[1])
    if t == "add":
        return delimcc.Add(to_ast(s[1]), to_ast(s[2]))
    if t == "lam":
        return delimcc.Lam(s[1], to_ast(s[2]))
    if t == "call":
        return delimcc.Call(to_ast(s[1]), to_ast(s[2]))
    if t == "prompt":
        return delimcc.Prompt(s[1], to_ast(s[2]))
    if t == "shift":
        return delimcc.Shift(s[1], s[2], to_ast(s[3]))
    if t == "wind":
        return delimcc.Wind(to_ast(s[1]), to_ast(s[2]), to_ast(s[3]))
    raise ValueError(f"未知模式: {s!r}")  # pragma: no cover
