"""运行时值。

- IntVal：整数结果
- CloVal：lambda 求值得到的词法闭包（持有不可变 AST body 的共享引用）
- ContVal：shift 捕获的定界续延（含 tag 与一帧不可变上下文），
  默认可多次恢复；shift_once 产生一次性实例（once=True），其消费
  状态是机器内部的记账，不改变语言的纯函数式语义

闭包与续延是不透明值：相等性按身份比较、有可读 repr；整数用
``isinstance(v, IntVal)`` 判别后取 ``v.value``。语言无可变引用，
值一经产生不再改变；续延值可多次调用，每次调用都不修改自身帧。
"""

from __future__ import annotations

from .errors import RuntimeTypeError


class Value:
    __slots__ = ()

    def display(self) -> str:
        raise NotImplementedError


class IntVal(Value):
    __slots__ = ("value",)

    def __init__(self, value: int):
        self.value = value

    def display(self) -> str:
        return str(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, IntVal) and self.value == other.value

    def __hash__(self) -> int:
        return hash(("IntVal", self.value))

    def __repr__(self) -> str:
        return f"IntVal({self.value})"


class CloVal(Value):
    """词法闭包。body 是不可变 AST 节点，多闭包共享同一引用无副作用。"""

    __slots__ = ("param", "body", "env")

    def __init__(self, param: str, body, env):
        self.param = param
        self.body = body
        self.env = env

    def display(self) -> str:
        return f"<lambda {self.param}>"

    def __eq__(self, other: object) -> bool:
        return other is self

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"CloVal(param={self.param!r})"


class ContVal(Value):
    """被 shift 捕获的续延值。

    tag: 重新包裹时使用的 prompt tag；frames: 捕获到的不可变帧元组
    （栈底->栈顶顺序）。帧结构属于解释器内部，不对外保证稳定。
    """

    __slots__ = ("tag", "frames", "once", "consumed")

    def __init__(self, tag: int, frames: tuple, *, once: bool = False):
        self.tag = tag
        self.frames = frames
        # once=True 时该对象全程序只能恢复一次。消费状态挂在对象身份上，
        # 故别名（传给函数、多重环境绑定）共享同一状态。
        self.once = once
        self.consumed = False

    def is_one_shot(self) -> bool:
        return self.once

    def is_consumed(self) -> bool:
        """一次性续延是否已经被成功恢复过；可重复续延恒为 False。"""
        return self.consumed

    def display(self) -> str:
        head = "once-continuation" if self.once else "continuation"
        state = ", consumed" if self.once and self.consumed else ""
        return f"<{head} tag={self.tag} depth={len(self.frames)}{state}>"

    def __eq__(self, other: object) -> bool:
        return other is self

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        once = ", once=True" if self.once else ""
        return f"ContVal(tag={self.tag!r}, depth={len(self.frames)}{once})"


def expect_int(v: Value, what: str) -> int:
    """要求运行时值为整数，否则抛 RuntimeTypeError（不静默转换）。"""
    if not isinstance(v, IntVal):
        if isinstance(v, CloVal):
            kind = "lambda"
        elif isinstance(v, ContVal):
            kind = "once-continuation" if v.is_one_shot() else "continuation"
        else:
            kind = type(v).__name__
        raise RuntimeTypeError(f"{what}需要 int，实际得到 {kind}: {v.display()}")
    return v.value
