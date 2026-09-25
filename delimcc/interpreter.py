"""定界续延解释器核心（CEK 风格显式栈迭代机器）。

语义契约
--------
* 按值、左到右求值；lambda 为词法闭包；语言中无可变引用。
* ``Prompt(tag, body)`` 在控制栈上压入 ``("prompt", tag)`` 定界帧。
* ``Shift(tag, k, body)`` 自栈顶向栈底寻找**最近**的同 tag 定界帧：
  - 找到：该帧以上（更靠近栈顶）的全部帧被捕获为不可变续延并绑定
    到 ``k``；定界帧保留，``body`` 在该 prompt **之内**继续求值。
  - 找不到：抛 ``UncaughtShiftError``（确定性语义错误，即无解）。
* ``ShiftOnce(tag, k, body)`` 捕获规则与 ``Shift`` 完全相同（最近同
  tag prompt、定界帧保留、body 在 prompt 内求值），但绑定到 ``k``
  的是**一次性句柄** ``OnceContVal``。
* 调用续延 ``k(v)`` 是**组合式**恢复：把捕获帧接到当前栈顶，其下
  重新压入同 tag 的定界帧，再以 v 驱动捕获栈；不丢弃调用点以下的
  任何帧。多次续延值不可变，可多次调用，每次行为一致。
* 一次性句柄的调用分解为可观察的小步：先求值函数与实参，实参求值
  完毕后才**原子地“检查并消费”**句柄（``OnceContVal.consume``），
  随后才压入重包 prompt 与捕获帧。因此：
  - 实参求值失败（含被其他 tag 的 shift 中断）时句柄尚未消费；
  - 进入截获上下文之后出错（类型错误、预算耗尽等）句柄仍然已消费，
    不存在回滚/复活路径；
  - 第二次进入抛 ``ContinuationConsumedError``，错误携带首次消费的
    来源位置与本次调用位置（来自 Call 节点的 loc）。
* “步”指一次机器迁移（消解一个求值点或处理一个返回帧）。超出
  ``max_steps`` 抛 ``StepLimitError``（预算耗尽，与语义无解严格
  区分）。节点数在求值前以迭代方式静态统计，超限是
  ``InvalidProgramError``。
* ``trace=True`` 时若求值以 ``EvaluationError`` 告终，异常对象上
  附带 ``.events``（截至失败点的部分事件轨迹），用于核对一次性
  句柄的消费时点。

解释器全程迭代执行，不使用 Python 递归，故嵌套深度只受步数预算约束。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import ast as A
from .errors import (
    EvaluationError,
    InvalidProgramError,
    RuntimeTypeError,
    StepLimitError,
    UnboundVariableError,
    UncaughtShiftError,
)
from .values import CloVal, ContVal, IntVal, OnceContVal, Value, expect_int

DEFAULT_MAX_NODES = 1000
DEFAULT_MAX_STEPS = 20000

# 环境为单绑定链表节点：None 或 (外层环境, 名字, 值)。扩展 O(1)，
# 闭包/续延只持有不可变的链头引用。
Env = Optional[Tuple["Env", str, Value]]

# 控制栈帧（栈底 -> 栈顶顺序保存于 tuple；帧结构为内部细节）：
#   ("addL", 右表达式, 求值右表达式时的环境)
#   ("addR", 左整数)
#   ("callFn", 参数表达式, 求值参数时的环境, 调用位置 loc)
#   ("callArg", 函数值, 调用位置 loc)
#   ("prompt", tag, 来源)  来源 "src" 为源码 Prompt，"rewrap" 为 k(v) 重包
Frame = tuple
Stack = Tuple[Frame, ...]


class Event:
    """一次求值事件（仅 ``interpret(..., trace=True)`` 时产生）。

    种类：
      ``prompt_enter(tag)`` / ``prompt_exit(tag, 值展示)``（源码 Prompt）
      ``prompt_rewrap_enter(tag)`` / ``prompt_rewrap_exit(tag, 值展示)``
      （仅由 k(v) 恢复时重新包裹产生）
      ``shift_capture(tag, 捕获帧数)``
      ``once_capture(tag, 捕获帧数)``（shift_once 的捕获）
      ``cont_call(tag, 续延帧数)``
      ``once_consume(tag, 续延帧数, 调用位置 loc)``（一次性句柄原子消费点；
      发生在实参求值完毕之后、重包 prompt 之前）
      ``add(左, 右, 和)``
      ``result(值展示)``
    """

    __slots__ = ("name", "args")

    def __init__(self, name: str, *args: object):
        self.name = name
        self.args = args

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Event)
            and self.name == other.name
            and self.args == other.args
        )

    def __hash__(self) -> int:
        return hash((self.name, self.args))

    def __repr__(self) -> str:
        return f"Event({self.name!r}, {', '.join(repr(a) for a in self.args)})"


class Result:
    """求值结果：值、事件轨迹（未请求 trace 时为空元组）、节点数、步数。"""

    __slots__ = ("value", "events", "nodes", "steps")

    def __init__(self, value: Value, events: Tuple[Event, ...], nodes: int, steps: int):
        self.value = value
        self.events = events
        self.nodes = nodes
        self.steps = steps

    def __repr__(self) -> str:
        return (
            f"Result(value={self.value!r}, nodes={self.nodes}, steps={self.steps}, "
            f"events={len(self.events)})"
        )


def _count_nodes(root: A.Expr) -> int:
    """迭代统计节点数，避免深树触发 Python 递归限制。"""
    n = 0
    stack = [root]
    while stack:
        e = stack.pop()
        n += 1
        stack.extend(e.children())
    return n


def _lookup(env: Env, name: str) -> Value:
    while env is not None:
        outer, n, val = env
        if n == name:
            return val
        env = outer
    raise UnboundVariableError(name)


def interpret(
    program: A.Expr,
    *,
    trace: bool = False,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Result:
    """求值一个程序表达式，返回 :class:`Result`。

    参数
    ----
    program:   AST 根节点（构造时已完成字段校验）。
    trace:     True 时在结果中记录求值事件。
    max_nodes: 节点数接口边界（默认 1000）。根树节点数超过它时抛
               ``InvalidProgramError``，求值不开始。
    max_steps: 求值步数接口边界（默认 20000）。超出抛
               ``StepLimitError``，与“无解”类错误区分。
    """
    if not isinstance(program, A.Expr):
        raise InvalidProgramError(
            f"interpret 的 program 必须是 Expr，得到 {type(program).__name__}"
        )
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes < 1:
        raise InvalidProgramError(f"max_nodes 必须是正整数，得到 {max_nodes!r}")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
        raise InvalidProgramError(f"max_steps 必须是正整数，得到 {max_steps!r}")

    n_nodes = _count_nodes(program)
    if n_nodes > max_nodes:
        raise InvalidProgramError(
            f"程序节点数 {n_nodes} 超过上限 {max_nodes}（接口边界，求值未开始）"
        )

    events: Optional[List[Event]] = [] if trace else None

    # 机器状态：mode="eval" 时 (e, env) 有效；mode="return" 时 v 有效。
    mode = "eval"
    e: A.Expr = program
    env: Env = None
    v: Value = IntVal(0)
    st: Stack = ()
    steps = 0

    try:
        while True:
            steps += 1
            if steps > max_steps:
                raise StepLimitError(max_steps)

            if mode == "eval":
                k = e.kind
                if k == "Int":
                    v = IntVal(e.value)  # type: ignore[attr-defined]
                    mode = "return"
                elif k == "Var":
                    v = _lookup(env, e.name)  # type: ignore[attr-defined]
                    mode = "return"
                elif k == "Lam":
                    v = CloVal(e.param, e.body, env)  # type: ignore[attr-defined]
                    mode = "return"
                elif k == "Add":
                    # 左到右：先求左，右表达式及其环境留帧。
                    st = st + (("addL", e.right, env),)  # type: ignore[attr-defined]
                    e = e.left  # type: ignore[attr-defined]
                elif k == "Call":
                    # 记录调用位置，供一次性句柄报告消费来源。
                    st = st + (("callFn", e.arg, env, e.loc),)  # type: ignore[attr-defined]
                    e = e.fn  # type: ignore[attr-defined]
                elif k == "Prompt":
                    tag = e.tag  # type: ignore[attr-defined]
                    if events is not None:
                        events.append(Event("prompt_enter", tag))
                    st = st + (("prompt", tag, "src"),)
                    e = e.body  # type: ignore[attr-defined]
                elif k == "Shift" or k == "ShiftOnce":
                    tag = e.tag  # type: ignore[attr-defined]
                    idx = -1
                    for j in range(len(st) - 1, -1, -1):
                        fr = st[j]
                        if fr[0] == "prompt" and fr[1] == tag:
                            idx = j
                            break
                    if idx < 0:
                        # 无解：不存在同 tag 的包围提示。
                        raise UncaughtShiftError(tag)
                    captured = st[idx + 1 :]
                    st = st[: idx + 1]  # 保留定界帧：body 在该 prompt 内求值
                    if k == "Shift":
                        if events is not None:
                            events.append(Event("shift_capture", tag, len(captured)))
                        cont: Value = ContVal(tag, captured)
                    else:
                        if events is not None:
                            events.append(Event("once_capture", tag, len(captured)))
                        cont = OnceContVal(tag, captured)
                    env = (env, e.param, cont)  # type: ignore[attr-defined]
                    e = e.body  # type: ignore[attr-defined]
                else:  # pragma: no cover - 构造器已穷尽种类
                    raise InvalidProgramError(f"内部错误：未知节点种类 {k!r}")

            else:  # mode == "return"：把 v 交回栈顶帧
                if not st:
                    if events is not None:
                        events.append(Event("result", v.display()))
                    return Result(v, tuple(events or ()), n_nodes, steps)
                fr = st[-1]
                st = st[:-1]
                fk = fr[0]

                if fk == "addL":
                    left_i = expect_int(v, "加法左值")
                    st = st + (("addR", left_i),)
                    e, env = fr[1], fr[2]
                    mode = "eval"
                elif fk == "addR":
                    right_i = expect_int(v, "加法右值")
                    s = fr[1] + right_i
                    if events is not None:
                        events.append(Event("add", fr[1], right_i, s))
                    v = IntVal(s)
                    # mode 保持 return：和继续上交。
                elif fk == "callFn":
                    if not isinstance(v, (CloVal, ContVal, OnceContVal)):
                        kind = "int" if isinstance(v, IntVal) else type(v).__name__
                        raise RuntimeTypeError(f"只能调用 lambda/continuation，得到 {kind}")
                    st = st + (("callArg", v, fr[3]),)
                    e, env = fr[1], fr[2]
                    mode = "eval"
                elif fk == "callArg":
                    fn: Value = fr[1]
                    if isinstance(fn, CloVal):
                        env = (fn.env, fn.param, v)
                        e = fn.body
                        mode = "eval"
                    elif isinstance(fn, ContVal):
                        if events is not None:
                            events.append(Event("cont_call", fn.tag, len(fn.frames)))
                            events.append(Event("prompt_rewrap_enter", fn.tag))
                        # 组合式恢复（tuple 为栈底->栈顶）：调用点上下文在最
                        # 外（保留不动），向内依次是重包的同 tag prompt 与捕获
                        # 帧；v 从最内的洞开始驱动捕获栈。
                        st = st + (("prompt", fn.tag, "rewrap"),) + fn.frames
                    else:  # OnceContVal；callFn 已排除其他类型
                        # 实参已求值完毕（v）：此处原子地“检查并消费”句柄，
                        # 然后才进入截获上下文。已消费则 consume 抛
                        # ContinuationConsumedError，截获上下文不会重新执行。
                        fn.consume(fr[2])
                        if events is not None:
                            events.append(Event("once_consume", fn.tag, len(fn.frames), fr[2]))
                            events.append(Event("prompt_rewrap_enter", fn.tag))
                        st = st + (("prompt", fn.tag, "rewrap"),) + fn.frames
                elif fk == "prompt":
                    if events is not None:
                        ename = "prompt_exit" if fr[2] == "src" else "prompt_rewrap_exit"
                        events.append(Event(ename, fr[1], v.display()))
                    # v 继续向外传播。
                else:  # pragma: no cover
                    raise InvalidProgramError(f"内部错误：未知栈帧 {fk!r}")
    except EvaluationError as exc:
        # 失败时把截至当前的部分轨迹挂到异常上，便于核对消费时点；
        # 不修改异常类型与消息，也不做任何状态回滚。
        if events is not None and not hasattr(exc, "events"):
            exc.events = tuple(events)  # type: ignore[attr-defined]
        raise
