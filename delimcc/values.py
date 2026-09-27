"""运行时值。

- IntVal：整数结果
- CloVal：lambda 求值得到的词法闭包（持有不可变 AST body 的共享引用）
- ContVal：shift 捕获的可复用定界续延（含 tag 与一帧不可变上下文）
- OneShotContVal：shift1 捕获的一次性定界续延；帧不可变，但带有恰好
  翻转一次的消费标志，全程序只能成功恢复一次

闭包与续延是不透明值：相等性按身份比较、有可读 repr；整数用
``isinstance(v, IntVal)`` 判别后取 ``v.value``。普通值与可复用续延
一经产生不再改变；一次性续延是唯一带（单向）状态的运行时值，其
消费状态挂在对象身份上，变量别名共享。
"""

from __future__ import annotations

from .errors import ContinuationAlreadyResumedError, RuntimeTypeError


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

    __slots__ = ("tag", "frames")

    def __init__(self, tag: int, frames: tuple):
        self.tag = tag
        self.frames = frames

    def display(self) -> str:
        return f"<continuation tag={self.tag} depth={len(self.frames)}>"

    def __eq__(self, other: object) -> bool:
        return other is self

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"ContVal(tag={self.tag!r}, depth={len(self.frames)})"


class OneShotContVal(Value):
    """shift1 捕获的一次性续延值。

    与 :class:`ContVal` 的唯一语义差别是只能成功恢复一次：``consume()``
    在恢复提交点被调用，标志翻转保证“成功恢复至多一次”。状态属于
    **对象身份**：把该值经参数传递/存入多个变量形成别名后，任一别名
    的成功恢复都会使其他别名上的再次调用抛出
    :class:`ContinuationAlreadyResumedError`。

    帧元组本身不可变；只有 spent 标志会恰好翻转一次。
    """

    __slots__ = ("tag", "frames", "spent")

    def __init__(self, tag: int, frames: tuple):
        self.tag = tag
        self.frames = frames
        self.spent = False

    def consume(self) -> None:
        """在恢复提交点声明消费；已消费则报错（不改动任何状态）。"""
        if self.spent:
            raise ContinuationAlreadyResumedError(self.tag)
        self.spent = True

    def display(self) -> str:
        state = "spent" if self.spent else "fresh"
        return f"<one-shot continuation tag={self.tag} depth={len(self.frames)} {state}>"

    def __eq__(self, other: object) -> bool:
        return other is self

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        state = "spent" if self.spent else "fresh"
        return f"OneShotContVal(tag={self.tag!r}, depth={len(self.frames)}, {state})"


def expect_int(v: Value, what: str) -> int:
    """要求运行时值为整数，否则抛 RuntimeTypeError（不静默转换）。"""
    if not isinstance(v, IntVal):
        if isinstance(v, CloVal):
            kind = "lambda"
        elif isinstance(v, OneShotContVal):
            kind = "one-shot continuation"
        elif isinstance(v, ContVal):
            kind = "continuation"
        else:
            kind = type(v).__name__
        raise RuntimeTypeError(f"{what}需要 int，实际得到 {kind}: {v.display()}")
    return v.value
