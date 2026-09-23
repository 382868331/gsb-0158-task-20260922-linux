"""定界续延解释器核心（CEK 风格显式栈迭代机器）。

语义契约
--------
* 按值、左到右求值；lambda 为词法闭包；语言中无可变引用。
* ``Prompt(tag, body)`` 在控制栈上压入 ``("prompt", tag)`` 定界帧。
* ``Shift(tag, k, body)`` 自栈顶向栈底寻找**最近**的同 tag 定界帧：
  - 找到：该帧以上（更靠近栈顶）的全部帧被捕获为不可变续延并绑定
    到 ``k``；定界帧保留，``body`` 在该 prompt **之内**继续求值。
  - 找不到：抛 ``UncaughtShiftError``（确定性语义错误，即无解）。
* 调用续延 ``k(v)`` 是**组合式**恢复：把捕获帧接到当前栈顶，其下
  重新压入同 tag 的定界帧，再以 v 驱动捕获栈；不丢弃调用点以下的
  任何帧。续延值不可变，因此可多次调用，每次行为一致。
* ``Wind(before, body, after)`` 是带进入/离开钩子的动态作用域：
  - 正常：before → body → after，Wind 的值取 body 的值（钩子值丢弃）；
  - body 以错误退出：由内向外执行离开作用域的 after，再重抛原错误；
  - ``shift`` 捕获跨越本作用域：作用域被离开，由内向外执行 after；
    ``k(v)`` 恢复重入时由外向内执行 before，洞值沿重放的捕获栈
    返回时再次由内向外执行 after（动态 wind 语义，进出严格配对）。
  钩子只用于记录事件：钩子里求值游离 ``shift`` 或调用续延值抛
  ``HookViolationError``；钩子表达式自身的其他错误（未绑定变量、
  类型错误等）直接抛出，before 失败则作用域从未进入（不跑 after），
  展开中的 after 失败则不再二次展开。
* “步”指一次机器迁移（消解一个求值点或处理一个返回帧，含钩子）。
  超出 ``max_steps`` 抛 ``StepLimitError``（预算耗尽立即中止，**不**
  展开 wind 钩子；与语义无解严格区分）。节点数在求值前以迭代方式
  静态统计，超限是 ``InvalidProgramError``。

解释器全程迭代执行，不使用 Python 递归，故嵌套深度只受步数预算约束。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import ast as A
from .errors import (
    EvaluationError,
    HookViolationError,
    InvalidProgramError,
    RuntimeTypeError,
    StepLimitError,
    UnboundVariableError,
    UncaughtShiftError,
)
from .values import CloVal, ContVal, IntVal, Value, expect_int

DEFAULT_MAX_NODES = 1000
DEFAULT_MAX_STEPS = 20000

# 环境为单绑定链表节点：None 或 (外层环境, 名字, 值)。扩展 O(1)，
# 闭包/续延只持有不可变的链头引用。
Env = Optional[Tuple["Env", str, Value]]

# 控制栈帧（栈底 -> 栈顶保存于 tuple；帧结构为内部细节）：
#   ("addL", 右表达式, 右表达式环境)
#   ("addR", 左整数)
#   ("callFn", 参数表达式, 参数环境)
#   ("callArg", 函数值)
#   ("prompt", tag, 来源)   来源 "src" 源码 / "rewrap" k(v) 重包
# Wind 正常生命周期：
#   ("windEntering", before, after, body, 进入环境)  before 求值中（保护帧）
#   ("windActive", before, after, 进入环境)  body 进行中（活动标记；
#       保留 before/after 表达式供捕获后重放）
#   ("windLeaving", after, 进入环境, body值)  正常 after 求值中（保护帧）
# 捕获离开 / 错误展开 / 恢复重入时的钩子链帧（也是钩子保护帧）：
#   ("hookAfter", after 表达式, 环境)
#   ("hookBefore", before 表达式, 环境)
# 钩子链跑完后的收尾帧：
#   ("afterShiftBody", k名, body, shift点环境, 续延值)
#   ("afterContResume", 捕获帧元组, 洞值)
#   ("afterReraise", 原异常)
Frame = tuple
Stack = Tuple[Frame, ...]

#: 钩子保护帧：栈上存在这种帧表示栈顶方向的求值处于某个钩子内。
HOOK_GUARD_KINDS = frozenset(
    {"windEntering", "windLeaving", "hookAfter", "hookBefore"}
)

#: 链式钩子帧（捕获离开 / 展开 / 重入时连续求值的钩子）。
HOOK_CHAIN_KINDS = frozenset({"hookAfter", "hookBefore"})


class Event:
    """一次求值事件（仅 ``interpret(..., trace=True)`` 时产生）。

    种类：
      ``prompt_enter(tag)`` / ``prompt_exit(tag, 值展示)``（源码 Prompt）
      ``prompt_rewrap_enter(tag)`` / ``prompt_rewrap_exit(tag, 值展示)``
      （仅由 k(v) 恢复时重新包裹产生）
      ``shift_capture(tag, 捕获帧数)``
      ``cont_call(tag, 续延帧数)``
      ``wind_before(钩子值展示)`` / ``wind_after(钩子值展示)``：每执行
      一次进入/离开钩子各发一个，参数是该钩子表达式的值（随后被丢弃），
      可用于区分嵌套作用域；正常进出、捕获离开、错误展开、恢复重入都
      计入，进出严格配对。
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


def _in_hook(st: Stack) -> bool:
    """当前求值点是否动态处于某个 wind 钩子内（栈上存在保护帧）。"""
    for fr in st:
        if fr[0] in HOOK_GUARD_KINDS:
            return True
    return False


def _find_prompt(st: Stack, tag: int) -> int:
    """自栈顶向栈底找最近同 tag prompt 帧下标，找不到返回 -1。"""
    for j in range(len(st) - 1, -1, -1):
        fr = st[j]
        if fr[0] == "prompt" and fr[1] == tag:
            return j
    return -1


def _active_scopes(st: Stack) -> List[Tuple[A.Expr, A.Expr, Env]]:
    """栈段中活动 wind 作用域，按栈底 -> 栈顶（外 -> 内）顺序。

    返回 ``(before 表达式, after 表达式, 进入环境)`` 列表。
    """
    return [(fr[1], fr[2], fr[3]) for fr in st if fr[0] == "windActive"]


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

    def emit(*a: object) -> None:
        if events is not None:
            events.append(Event(*a))

    def escape(exc: EvaluationError) -> "EvaluationError":
        """错误逃逸出机器前，附上 trace 已记录的事件（含错误展开钩子）。"""
        if events is not None:
            exc.events = tuple(events)  # type: ignore[attr-defined]
        return exc

    # 机器状态：mode="eval" 时 (e, env) 有效；mode="return" 时 v 有效。
    mode = "eval"
    e: A.Expr = program
    env: Env = None
    v: Value = IntVal(0)
    st: Stack = ()
    steps = 0

    while True:
        steps += 1
        if steps > max_steps:
            # 资源预算中止：立即抛出，不展开任何 wind 钩子。
            raise escape(StepLimitError(max_steps))

        try:
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
                    st = st + (("callFn", e.arg, env),)  # type: ignore[attr-defined]
                    e = e.fn  # type: ignore[attr-defined]
                elif k == "Prompt":
                    tag = e.tag  # type: ignore[attr-defined]
                    emit("prompt_enter", tag)
                    st = st + (("prompt", tag, "src"),)
                    e = e.body  # type: ignore[attr-defined]
                elif k == "Wind":
                    # 进入作用域：先求 before（保护帧标记钩子上下文）。
                    st = st + (
                        (
                            "windEntering",
                            e.before,  # type: ignore[attr-defined]
                            e.after,  # type: ignore[attr-defined]
                            e.body,  # type: ignore[attr-defined]
                            env,
                        ),
                    )
                    e = e.before  # type: ignore[attr-defined]
                elif k == "Shift":
                    tag = e.tag  # type: ignore[attr-defined]
                    if _in_hook(st):
                        raise HookViolationError(
                            "before/after", f"shift(tag={tag}) 捕获续延"
                        )
                    idx = _find_prompt(st, tag)
                    if idx < 0:
                        # 无解：不存在同 tag 的包围提示。
                        raise UncaughtShiftError(tag)
                    captured = st[idx + 1 :]
                    st = st[: idx + 1]  # 保留定界帧：body 在该 prompt 内
                    emit("shift_capture", tag, len(captured))
                    cont = ContVal(tag, captured)
                    # 被捕获段中的活动 wind 作用域随捕获离开：由内向外
                    # 执行 after（scopes 为外 -> 内；正向压栈使最内在顶）。
                    scopes = _active_scopes(captured)
                    if scopes:
                        st = st + (
                            (
                                "afterShiftBody",
                                e.param,  # type: ignore[attr-defined]
                                e.body,  # type: ignore[attr-defined]
                                env,
                                cont,
                            ),
                        )
                        for _b, after_expr, hook_env in scopes:
                            st = st + (("hookAfter", after_expr, hook_env),)
                        e, env = st[-1][1], st[-1][2]
                        # mode 保持 eval：从最内层 after 开始。
                    else:
                        env = (env, e.param, cont)  # type: ignore[attr-defined]
                        e = e.body  # type: ignore[attr-defined]
                else:  # pragma: no cover - 构造器已穷尽种类
                    raise InvalidProgramError(f"内部错误：未知节点种类 {k!r}")

            else:  # mode == "return"：把 v 交回栈顶帧
                if not st:
                    emit("result", v.display())
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
                    emit("add", fr[1], right_i, s)
                    v = IntVal(s)
                    # mode 保持 return：和继续上交。
                elif fk == "callFn":
                    if isinstance(v, ContVal) and _in_hook(st):
                        raise HookViolationError("before/after", "调用续延 k(v)")
                    if not isinstance(v, (CloVal, ContVal)):
                        kind = "int" if isinstance(v, IntVal) else type(v).__name__
                        raise RuntimeTypeError(
                            f"只能调用 lambda/continuation，得到 {kind}"
                        )
                    st = st + (("callArg", v),)
                    e, env = fr[1], fr[2]
                    mode = "eval"
                elif fk == "callArg":
                    fn: Value = fr[1]
                    if isinstance(fn, CloVal):
                        env = (fn.env, fn.param, v)
                        e = fn.body
                        mode = "eval"
                    else:  # ContVal；callFn 已排除钩子内调用与其他类型
                        emit("cont_call", fn.tag, len(fn.frames))
                        emit("prompt_rewrap_enter", fn.tag)
                        # 恢复重入：先重包同 tag prompt，再对捕获段中的
                        # 活动作用域由外向内执行 before，最后拼接捕获帧
                        # 并以洞值 v 驱动（捕获栈中的 windActive 帧会在
                        # 值返回途中自然地再跑一次 after）。
                        scopes = _active_scopes(fn.frames)
                        st = st + (("prompt", fn.tag, "rewrap"),)
                        if scopes:
                            st = st + (("afterContResume", fn.frames, v),)
                            # 外 -> 内执行：逆序压栈使最外在栈顶。
                            for before_expr, _a, hook_env in reversed(scopes):
                                st = st + (("hookBefore", before_expr, hook_env),)
                            e, env = st[-1][1], st[-1][2]
                            mode = "eval"
                        else:
                            st = st + fn.frames
                            # mode 保持 return：洞值上交捕获栈最内帧。
                elif fk == "windEntering":
                    # before 完成：记录事件（值丢弃），压活动标记并求 body。
                    _, before_expr, after_expr, body_expr, enter_env = fr
                    emit("wind_before", v.display())
                    st = st + (("windActive", before_expr, after_expr, enter_env),)
                    e, env = body_expr, enter_env
                    mode = "eval"
                elif fk == "windActive":
                    # body 正常完成：离开作用域，求 after（保护帧），
                    # body 值暂存帧内，after 完成后原样上交。
                    _, _before_expr, after_expr, enter_env = fr
                    st = st + (("windLeaving", after_expr, enter_env, v),)
                    e, env = after_expr, enter_env
                    mode = "eval"
                elif fk == "windLeaving":
                    # 正常 after 完成：记录事件（值丢弃），恢复 body 值。
                    emit("wind_after", v.display())
                    v = fr[3]
                elif fk in HOOK_CHAIN_KINDS:
                    # 链式钩子中的一个完成：记录事件（值丢弃），驱动链上
                    # 下一个钩子；链尾交给收尾帧。
                    emit(
                        "wind_after" if fk == "hookAfter" else "wind_before",
                        v.display(),
                    )
                    if st and st[-1][0] in HOOK_CHAIN_KINDS:
                        nf = st[-1]
                        # 不取帧：下一轮 return 上交时再弹，保持“完成才弹”。
                        e, env = nf[1], nf[2]
                        mode = "eval"
                    elif st and st[-1][0] == "afterShiftBody":
                        tf = st[-1]
                        st = st[:-1]
                        _, kparam, body_expr, shift_env, cont = tf
                        env = (shift_env, kparam, cont)
                        e = body_expr
                        mode = "eval"
                    elif st and st[-1][0] == "afterContResume":
                        tf = st[-1]
                        st = st[:-1]
                        _, cap_frames, hole = tf
                        st = st + cap_frames
                        v = hole
                        # mode 保持 return：洞值上交重放的捕获栈。
                    elif st and st[-1][0] == "afterReraise":
                        tf = st[-1]
                        st = st[:-1]
                        raise tf[1]
                    else:  # pragma: no cover - 链帧总有收尾帧
                        raise InvalidProgramError("内部错误：wind 钩子链缺少收尾帧")
                elif fk == "prompt":
                    ename = "prompt_exit" if fr[2] == "src" else "prompt_rewrap_exit"
                    emit(ename, fr[1], v.display())
                    # v 继续向外传播。
                else:  # pragma: no cover
                    raise InvalidProgramError(f"内部错误：未知栈帧 {fk!r}")

        except StepLimitError:
            raise  # 已在 raise 处附事件；预算中止绝不展开钩子。
        except EvaluationError as exc:
            # 已在钩子内（钩子自身出错，或展开链中的钩子出错）：直接抛，
            # 不做二次展开；before 失败时作用域从未进入。
            if _in_hook(st):
                raise escape(exc)
            # body 正常求值出错：放弃当前计算，离开所有活动 wind 作用域，
            # 由内向外跑 after，最后重抛原错误。无活动作用域则直接抛。
            scopes = _active_scopes(st)
            if not scopes:
                raise escape(exc)
            st = (("afterReraise", exc),)
            # 外 -> 内压栈，最内在栈顶最先执行。
            for _b, after_expr, hook_env in scopes:
                st = st + (("hookAfter", after_expr, hook_env),)
            e, env = st[-1][1], st[-1][2]
            mode = "eval"
