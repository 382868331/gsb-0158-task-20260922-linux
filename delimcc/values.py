"""运行时值（不可变）。

- IntVal：整数结果
- CloVal：lambda 求值得到的词法闭包（持有不可变 AST body 的共享引用）
- ContVal：shift 捕获的定界续延（含 tag 与一帧不可变上下文）
- OnceContVal：shift1 捕获的一次性续延；帧本身不可变，但句柄带一个
  所有别名共享的消费单元，至多允许进入一次

闭包与续延是不透明值：相等性按身份比较、有可读 repr；整数用
``isinstance(v, IntVal)`` 判别后取 ``v.value``。语言无可变引用。
普通续延值可多次调用，每次调用都不修改自身帧；一次性续延在第一次
进入的原子检查点被消费，之后任何别名再次进入都会报错。
"""

from __future__ import annotations

from typing import Optional

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
    """shift1 捕获的一次性续延句柄。

    与 :class:`ContVal` 一样持有不可变的 tag 与捕获帧；区别在于句柄
    带一个可变的消费单元（``_cell`` 的首元素）。值可以被任意别名
    引用（lambda 参数传递、放入环境多次取出等），别名共享同一单元：

    - ``consume()`` 在“实参已求值完毕、即将进入续延”的原子检查点
      调用：未消费则翻转为已消费并返回 True（随后的上下文恢复即使
      出错，消费状态也不回滚）；已消费则返回 False。
    - ``consumed`` 属性只读观察消费状态；``first_call_loc`` 记录
      首次消费的调用点位置（可能为 None）。

    ``capture_loc`` 是 shift1 截获点的位置，仅用于错误定位。
    """

    __slots__ = ("tag", "frames", "capture_loc", "_cell")

    def __init__(self, tag: int, frames: tuple, capture_loc: Optional[tuple] = None):
        self.tag = tag
        self.frames = frames
        self.capture_loc = capture_loc
        # 单元素 list 即消费单元：False=未消费，True=已消费。所有别名
        # （同一对象的多个环境绑定）看到的是同一个 list。
        self._cell = [False, None]

    @property
    def consumed(self) -> bool:
        return self._cell[0]

    @property
    def first_call_loc(self) -> Optional[tuple]:
        return self._cell[1]

    def consume(self, call_loc: Optional[tuple]) -> bool:
        """原子检查并消费。未消费返回 True；已消费返回 False（不改状态）。"""
        if self._cell[0]:
            return False
        self._cell[0] = True
        self._cell[1] = call_loc
        return True

    def display(self) -> str:
        state = "consumed" if self._cell[0] else "fresh"
        return f"<once-continuation tag={self.tag} depth={len(self.frames)} {state}>"

    def __eq__(self, other: object) -> bool:
        return other is self

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        state = "consumed" if self._cell[0] else "fresh"
        return f"OnceContVal(tag={self.tag!r}, depth={len(self.frames)}, {state})"


def expect_int(v: Value, what: str) -> int:
    """要求运行时值为整数，否则抛 RuntimeTypeError（不静默转换）。"""
    if not isinstance(v, IntVal):
        if isinstance(v, CloVal):
            kind = "lambda"
        elif isinstance(v, OnceContVal):
            kind = "once-continuation"
        elif isinstance(v, ContVal):
            kind = "continuation"
        else:
            kind = type(v).__name__
        raise RuntimeTypeError(f"{what}需要 int，实际得到 {kind}: {v.display()}")
    return v.value
