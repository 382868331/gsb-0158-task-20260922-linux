"""运行时值。

- IntVal：整数结果
- CloVal：lambda 求值得到的词法闭包（持有不可变 AST body 的共享引用）
- ContVal：shift 捕获的定界续延（含 tag 与一帧不可变上下文），可多次进入
- OnceContVal：shift_once 捕获的一次性续延句柄，只能进入一次

闭包与续延是不透明值：相等性按身份比较、有可读 repr；整数用
``isinstance(v, IntVal)`` 判别后取 ``v.value``。语言无可变引用，
值一经产生不再改变；多次续延可多次调用，每次调用都不修改自身帧。

唯一的例外是一次性句柄的消费标记：这是“只能进入一次”语义的载体。
句柄捕获的上下文本身不可变，可变的只有“是否已消费”这一个单调
（False -> True，不可回退）的状态位；句柄不提供任何重置接口。
"""

from __future__ import annotations

from .errors import ContinuationConsumedError, RuntimeTypeError


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


class OnceContVal(Value):
    """shift_once 捕获的一次性续延句柄。

    tag/frames 含义与 ContVal 相同且不可变；唯一可变状态是消费标记
    （单调 False -> True）。``consume(loc)`` 原子地完成“检查并消费”：
    已消费则抛 ``ContinuationConsumedError``（携带首次消费位置与本次
    位置），否则置位并记录消费来源位置。解释器在调用参数求值完毕、
    即将进入截获上下文之前调用它——进入之后无论出错还是预算耗尽，
    都不存在让句柄复活的回滚路径。
    """

    __slots__ = ("tag", "frames", "_consumed", "_consumed_loc")

    def __init__(self, tag: int, frames: tuple):
        self.tag = tag
        self.frames = frames
        self._consumed = False
        self._consumed_loc = None

    @property
    def consumed(self) -> bool:
        """是否已被消费（只读观察，不提供重置）。"""
        return self._consumed

    @property
    def consumed_loc(self):
        """首次消费该句柄的调用位置（loc 三元组或 None）。"""
        return self._consumed_loc

    def consume(self, loc) -> None:
        """原子地检查并消费句柄；第二次及以后的调用抛错。"""
        if self._consumed:
            raise ContinuationConsumedError(self.tag, self._consumed_loc, loc)
        self._consumed = True
        self._consumed_loc = loc

    def display(self) -> str:
        state = "已消费" if self._consumed else "未消费"
        return f"<once-continuation tag={self.tag} depth={len(self.frames)} {state}>"

    def __eq__(self, other: object) -> bool:
        return other is self

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"OnceContVal(tag={self.tag!r}, depth={len(self.frames)}, consumed={self._consumed})"


def expect_int(v: Value, what: str) -> int:
    """要求运行时值为整数，否则抛 RuntimeTypeError（不静默转换）。"""
    if not isinstance(v, IntVal):
        if isinstance(v, CloVal):
            kind = "lambda"
        elif isinstance(v, ContVal):
            kind = "continuation"
        elif isinstance(v, OnceContVal):
            kind = "once-continuation"
        else:
            kind = type(v).__name__
        raise RuntimeTypeError(f"{what}需要 int，实际得到 {kind}: {v.display()}")
    return v.value
