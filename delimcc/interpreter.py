"""定界续延解释器核心（CEK 风格显式栈迭代机器）。

语义契约
--------
* 按值、左到右求值；lambda 为词法闭包；语言中无可变引用。
* ``Prompt(tag, body)`` 在控制栈上压入 ``("prompt", tag)`` 定界帧。
* ``Shift(tag, k, body)`` 自栈顶向栈底寻找**最近**的同 tag 定界帧：
  - 找到：该帧以上（更靠近栈顶）的全部帧被捕获为不可变续延并绑定
    到 ``k``；定界帧保留，``body`` 在该 prompt **之内**继续求值。
  - 找不到：按“异常退出”处理（见下），最终抛 ``UncaughtShiftError``。
* 调用续延 ``k(v)`` 是**组合式**恢复：把捕获帧接到当前栈顶，其下
  重新压入同 tag 的定界帧，再以 v 驱动捕获栈；不丢弃调用点以下的
  任何帧。续延值不可变，因此可多次调用，每次行为一致。
* ``Wind(before, body, after)`` 是动态作用域钩子：
  - **正常进入/返回**：进入作用域先求 ``before``（值丢弃）；body
    正常返回后求 ``after``（值丢弃），Wind 的值取 body 的值。
  - **异常退出**：求值错误向外传播穿过仍激活的作用域时，自内向外
    逐个求其 ``after``（reason=``"error"``），随后抛出原错误；
    ``before`` 自身失败时该作用域视为未进入，不执行其 ``after``；
    error 钩子自身失败则以新错误取代原错误继续展开。
  - **shift 捕获跨过作用域**：被捕获上下文内处于激活状态的 Wind
    作用域随捕获一起**离开**：栈截断提交后、shift body 开始前，
    自内向外执行这些作用域的 ``after``（reason=``"capture"``）。
  - **k(v) 恢复重入作用域**：捕获时离开的作用域随恢复**重新进入**：
    驱动捕获帧之前自外向内执行 ``before``（reason=``"resume"``）；
    v 穿过捕获上下文再次离开该作用域时照常执行
    ``after``（reason=``"normal"``）。被一起捕获的**异 tag 源码 prompt
    帧**也在每次恢复时重新建立（重发进入/退出事件）——续延是可复制
    快照，与定义式语义 ``k = λx. prompt(F[x])`` 一致；wind 的 before
    与这些 prompt 的进入按捕获帧外->内顺序交错重放。
  - 钩子表达式中静态禁止 Shift（AST 构造期递归保证，含 lambda 体内），
    故任何钩子求值期间都不会发生新的捕获。
* “步”指一次机器迁移（消解一个求值点或处理一个返回帧，钩子求值同
  样计步）。超出 ``max_steps`` 抛 ``StepLimitError``（预算耗尽，与
  语义无解严格区分）。步数预算是资源终止而非语言内异常，**不**触发
  error 钩子。

错误对象在 ``trace=True`` 时附带 ``events`` 属性，保留失败前已记录
的事件轨迹（trace=False 时为 ``()``）；不存在部分提交的结果值。

解释器全程迭代执行，不使用 Python 递归，故嵌套深度只受步数预算约束。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import ast as A
from .errors import (
    EvaluationError,
    HookShiftError,
    InvalidProgramError,
    RuntimeTypeError,
    StepLimitError,
    UnboundVariableError,
    UncaughtShiftError,
)
from .values import CloVal, ContVal, IntVal, Value

DEFAULT_MAX_NODES = 1000
DEFAULT_MAX_STEPS = 20000

# 环境为单绑定链表节点：None 或 (外层环境, 名字, 值)。扩展 O(1)，
# 闭包/续延只持有不可变的链头引用。
Env = Optional[Tuple["Env", str, Value]]

# 控制栈帧（栈底 -> 栈顶保存于 tuple；帧结构为内部细节）：
#   ("addL", 右表达式, 环境)
#   ("addR", 左整数)
#   ("callFn", 参数表达式, 环境)
#   ("callArg", 函数值)
#   ("prompt", tag, 来源)   来源 "src" / "rewrap"
# Wind 相关（active/boundary 的字段位置一致，便于捕获时统一转换）：
#   ("windBefore", before, body, after, 环境, id)  before 求值中（未进入）
#   ("windActive", before, after, 环境, id)        body 求值中（已进入）
#   ("windBoundary", before, after, 环境, id)      捕获快照中的作用域边界
#   ("windAfter", 穿域值)                          正常离开时 after 求值中
#   ("windBoundaryAfter", 穿域值)                  恢复后离开时 after 求值中
#   ("windCaptureSeq", 含当前的剩余 after 任务, 完成载荷, 拆卸期错误)
#   ("replay", 剩余重放任务(含在途), 洞值)  恢复时统一重放进入侧
#   ("windErrorCont", 原错误)                      error 钩子求值后续展
Frame = tuple
Stack = Tuple[Frame, ...]

# (id, before, after, env)：捕获时离开、恢复时重入的作用域记录（外->内）。
Scope = Tuple[int, A.Expr, A.Expr, Env]
# (id, after 表达式, 环境)：捕获离开序列任务（内->外）。
AfterJob = Tuple[int, A.Expr, Env]
# 捕获序列全部完成后的续延载荷（不含 scopes：恢复序列由捕获帧现算）。
CapturePayload = Tuple[int, str, A.Expr, Env, tuple]


class Event:
    """一次求值事件（仅 ``interpret(..., trace=True)`` 时产生）。

    种类：
      ``prompt_enter(tag)`` / ``prompt_exit(tag, 值展示)``（源码 Prompt）
      ``prompt_rewrap_enter(tag)`` / ``prompt_rewrap_exit(tag, 值展示)``
      （仅由 k(v) 恢复时重新包裹产生）
      ``shift_capture(tag, 捕获帧数)``
      ``cont_call(tag, 续延帧数)``
      ``wind_before(id, why)``：why=``"enter"``（正常进入）或
      ``"resume"``（续延恢复重入）
      ``wind_after(id, why)``：why=``"normal"``（正常返回离开）、
      ``"capture"``（被 shift 捕获带走）、``"error"``（异常退出）
      ``add(左, 右, 和)``
      ``result(值展示)``

    同一作用域实例在正常进入、被捕获与每次恢复重入时使用相同的
    ``id``，便于配对观察（配合 why 区分阶段）。
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


def _kind(v: Value) -> str:
    if isinstance(v, IntVal):
        return "int"
    if isinstance(v, CloVal):
        return "lambda"
    if isinstance(v, ContVal):
        return "continuation"
    return type(v).__name__


def _resume_replay(frames: Tuple[Frame, ...]) -> Tuple[tuple, ...]:
    """从捕获帧（外->内）构造恢复重放任务序列。

    续延是可复制快照：每次 k(v) 恢复都重新建立被一起捕获的全部定界
    帧，故 windBoundary 需重入 before，被捕获的 prompt 帧（含异 tag
    源码 prompt 与内层重包 prompt）需重发 enter。任务项：
      ``("wind", id, before 表达式, 环境)``
      ``("prompt", tag, 来源)``
    """
    jobs: List[tuple] = []
    for f in frames:
        if f[0] == "windBoundary":
            jobs.append(("wind", f[4], f[1], f[3]))
        elif f[0] == "prompt":
            jobs.append(("prompt", f[1], f[2]))
    return tuple(jobs)


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
    trace:     True 时在结果（或失败错误对象的 ``events`` 属性）中
               记录求值事件。
    max_nodes: 节点数接口边界（默认 1000）。根树节点数超过它时抛
               ``InvalidProgramError``，求值不开始。
    max_steps: 求值步数接口边界（默认 20000）。超出抛
               ``StepLimitError``，与“无解”类错误区分；预算耗尽不
               触发 error 钩子。
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

    def emit(name: str, *args: object) -> None:
        if events is not None:
            events.append(Event(name, *args))

    def attach(exc: EvaluationError) -> EvaluationError:
        exc.events = tuple(events) if events is not None else ()
        return exc

    # mode: "eval"（消解 e）/ "return"（上交 v）/ "error"（展开 err）。
    mode = "eval"
    err: Optional[EvaluationError] = None
    e: A.Expr = program
    env: Env = None
    v: Value = IntVal(0)
    st: Stack = ()
    steps = 0
    next_scope_id = 0

    while True:
        steps += 1
        if steps > max_steps:
            # 资源终止不是语言内异常：不触发 error 钩子。
            raise attach(StepLimitError(max_steps))  # type: ignore[arg-type]

        if mode == "error":
            # skip：恢复重放序列中尚未成功进入的作用域 id（含在途且刚
            # 失败的 before）——这些边界帧未进入，异常时直接放弃，不跑钩子。
            skip: set = set()
            for f0 in st:
                if f0[0] == "replay":
                    # 嵌套恢复时栈上可能有多个 replay 帧，取并集。
                    skip |= {job[1] for job in f0[1] if job[0] == "wind"}
            # 自栈顶丢弃不拦截异常的帧，直到遇到：仍激活的作用域
            # （执行 error-after），或捕获离开序列（继续外层拆卸）。
            while st:
                top = st[-1]
                fk = top[0]
                if fk == "windCaptureSeq":
                    # 在途 capture-after 失败：捕获中止，续延不生成。外层
                    # 仍被带走的作用域改按“异常退出(error)”自内向外拆卸；
                    # 全部拆完后该错误继续向 prompt 之外传播。
                    pending = top[1]
                    payload = top[2]
                    st = st[:-1]
                    if pending:
                        n0 = pending[0]
                        st = st + (("windCaptureSeq", pending[1:], payload, err),)
                        emit("wind_after", n0[0], "error")
                        e, env, mode = n0[1], n0[2], "eval"
                        break
                    # 拆卸已尽：回到统一 error 展开，继续处理 prompt 之外
                    # 仍激活的作用域（err 为最新错误）。
                    continue
                if fk in ("windActive", "windBoundary") and top[4] not in skip:
                    _, _, after_e, scope_env, scope_id = top
                    st = st[:-1]
                    emit("wind_after", scope_id, "error")
                    st = st + (("windErrorCont", err),)
                    e, env, mode = after_e, scope_env, "eval"
                    break
                st = st[:-1]
            else:
                assert err is not None
                raise attach(err)
            continue

        if mode == "replay":
            # 栈顶为 ("replay", tasks, hole)：恢复洞值交付前，
            # 按外->内补进入侧效果。前导 prompt 是纯事件（同步连发），
            # 遇到 wind 任务则异步求其 before；任务耗尽即交付洞值。
            rframe = st[-1]
            tasks: Tuple[tuple, ...] = rframe[1]
            hole: Value = rframe[2]
            while tasks and tasks[0][0] == "prompt":
                emit("prompt_enter", tasks[0][1])
                tasks = tasks[1:]
            st = st[:-1]
            if not tasks:
                v, mode = hole, "return"
                continue
            t0 = tasks[0]
            _, wid, beforex, envx = t0
            emit("wind_before", wid, "resume")
            # 保留完整 tasks（tasks[0] 即这个在途 wind）：before 返回时
            # 消费首个任务；异常时 tasks 内的 wind id 即“尚未进入”的。
            st = st + (("replay", tasks, hole),)
            e, env, mode = beforex, envx, "eval"
            continue

        if mode == "eval":
            k = e.kind
            if k == "Int":
                v = IntVal(e.value)  # type: ignore[attr-defined]
                mode = "return"
            elif k == "Var":
                try:
                    v = _lookup(env, e.name)  # type: ignore[attr-defined]
                except UnboundVariableError as ex:
                    err, mode = ex, "error"
                    continue
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
                scope_id = next_scope_id
                next_scope_id += 1
                before_e, body_e, after_e = e.before, e.body, e.after  # type: ignore[attr-defined]
                emit("wind_before", scope_id, "enter")
                st = st + (("windBefore", before_e, body_e, after_e, env, scope_id),)
                e = before_e
            elif k == "Shift":
                tag = e.tag  # type: ignore[attr-defined]
                # 钩子求值期间（含钩子里新建 prompt 的情形）动态触发
                # shift：钩子不捕获续延，确定性语义错误。取最内层标记
                # （钩子里允许再出现 Wind，标记可能嵌套）。
                hook_phase = None
                for f0 in reversed(st):
                    hk = f0[0]
                    if hk == "windBefore":
                        hook_phase = "before（进入）"
                        break
                    if hk == "windAfter":
                        hook_phase = "after（正常离开）"
                        break
                    if hk == "windBoundaryAfter":
                        hook_phase = "after（恢复后离开）"
                        break
                    if hk == "windCaptureSeq":
                        hook_phase = "after（捕获离开）"
                        break
                    if hk == "replay":
                        hook_phase = "before（恢复重入）"
                        break
                    if hk == "windErrorCont":
                        hook_phase = "after（异常退出）"
                        break
                if hook_phase is not None:
                    err, mode = HookShiftError(tag, hook_phase), "error"
                    continue
                idx = -1
                for j in range(len(st) - 1, -1, -1):
                    fr0 = st[j]
                    if fr0[0] == "prompt" and fr0[1] == tag:
                        idx = j
                        break
                if idx < 0:
                    # 无解：不存在同 tag 的包围提示；按异常退出展开作用域。
                    err, mode = UncaughtShiftError(tag), "error"
                    continue
                captured = st[idx + 1 :]
                # 唯一的语义提交点：截断捕获帧、保留 prompt 帧。随后执行
                # capture-after；任一钩子失败即按异常展开，续延不会生成。
                st = st[: idx + 1]
                # captured 为栈底->栈顶（外->内），作用域帧天然外->内有序。
                scopes: Tuple[Scope, ...] = tuple(
                    (f[4], f[1], f[2], f[3])
                    for f in captured
                    if f[0] in ("windActive", "windBoundary")
                )
                cont_frames = tuple(
                    ("windBoundary", f[1], f[2], f[3], f[4])
                    if f[0] in ("windActive", "windBoundary")
                    else ("prompt", f[1], "src")
                    if f[0] == "prompt"
                    else f
                    for f in captured
                )
                emit("shift_capture", tag, len(captured))
                if scopes:
                    jobs: Tuple[AfterJob, ...] = tuple(
                        (sid, afterx, envx) for sid, _, afterx, envx in reversed(scopes)
                    )
                    payload: CapturePayload = (
                        tag,
                        e.param,  # type: ignore[attr-defined]
                        e.body,  # type: ignore[attr-defined]
                        env,
                        cont_frames,
                    )
                    # pending=尚未开始的外层任务；第四个槽记录拆卸期错误。
                    st = st + (("windCaptureSeq", jobs[1:], payload, None),)
                    sid0, after0, env0 = jobs[0]
                    emit("wind_after", sid0, "capture")
                    e, env = after0, env0
                else:
                    cont = ContVal(tag, cont_frames)
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
                if not isinstance(v, IntVal):
                    err = RuntimeTypeError(f"加法左值需要 int，实际得到 {_kind(v)}: {v.display()}")
                    mode = "error"
                    continue
                st = st + (("addR", v.value),)
                e, env = fr[1], fr[2]
                mode = "eval"
            elif fk == "addR":
                if not isinstance(v, IntVal):
                    err = RuntimeTypeError(f"加法右值需要 int，实际得到 {_kind(v)}: {v.display()}")
                    mode = "error"
                    continue
                s = fr[1] + v.value
                emit("add", fr[1], v.value, s)
                v = IntVal(s)
                # mode 保持 return：和继续上交。
            elif fk == "callFn":
                if not isinstance(v, (CloVal, ContVal)):
                    err = RuntimeTypeError(f"只能调用 lambda/continuation，得到 {_kind(v)}")
                    mode = "error"
                    continue
                st = st + (("callArg", v),)
                e, env = fr[1], fr[2]
                mode = "eval"
            elif fk == "callArg":
                fn: Value = fr[1]
                if isinstance(fn, CloVal):
                    env = (fn.env, fn.param, v)
                    e = fn.body
                    mode = "eval"
                else:  # ContVal；callFn 已排除其他类型
                    emit("cont_call", fn.tag, len(fn.frames))
                    emit("prompt_rewrap_enter", fn.tag)
                    # 组合式恢复（tuple 栈底->栈顶）：调用点上下文在最外
                    # （保留不动），向内依次是重包 prompt 与捕获帧（其中
                    # 异 tag prompt 帧与 windBoundary 边界帧已在 fn.frames，
                    # 负责退出侧）。洞值驱动前先按外->内补进入侧效果。
                    st = st + (("prompt", fn.tag, "rewrap"),) + fn.frames
                    tasks = _resume_replay(fn.frames)
                    st = st + (("replay", tasks, v),)
                    # pump 会重放前导 prompt 进入、首个 wind before 或直接
                    # 交付洞值；通过 continue 进入下一轮。
                    mode = "replay"
                    continue
            elif fk == "replay":
                # 一个 resume-before 正常完成：消费首个 wind 任务，回到
                # replay 模式继续处理后续前导 prompt / wind / 交付洞值。
                tasks = fr[1]
                hole = fr[2]
                assert tasks and tasks[0][0] == "wind"
                st = st + (("replay", tasks[1:], hole),)
                mode = "replay"
            elif fk == "prompt":
                ename = "prompt_exit" if fr[2] == "src" else "prompt_rewrap_exit"
                emit(ename, fr[1], v.display())
                # v 继续向外传播。
            elif fk == "windBefore":
                # before 钩子完成，值丢弃；作用域正式激活，在 Wind 现场
                # 环境中求 body（before/after 也在同一现场环境）。
                _, before_e, body_e, after_e, scope_env, scope_id = fr
                st = st + (("windActive", before_e, after_e, scope_env, scope_id),)
                e, env, mode = body_e, scope_env, "eval"
            elif fk == "windActive":
                # body 正常完成：求 after，期间保存 body 的值。
                _, _, after_e, scope_env, scope_id = fr
                emit("wind_after", scope_id, "normal")
                st = st + (("windAfter", v),)
                e, env = after_e, scope_env
                mode = "eval"
            elif fk == "windAfter":
                # after 钩子完成，值丢弃；Wind 的值为保存的 body 值。
                v = fr[1]
                # mode 保持 return。
            elif fk == "windBoundary":
                # 恢复重入后的洞值正常穿过作用域边界：再执行一次 after。
                _, _, after_e, scope_env, scope_id = fr
                emit("wind_after", scope_id, "normal")
                st = st + (("windBoundaryAfter", v),)
                e, env = after_e, scope_env
                mode = "eval"
            elif fk == "windBoundaryAfter":
                v = fr[1]  # 穿域值继续上交，after 的值丢弃。
            elif fk == "windCaptureSeq":
                # 一个拆卸 after 正常完成。
                pending: Tuple[AfterJob, ...] = fr[1]
                payload: CapturePayload = fr[2]
                fail_err: Optional[EvaluationError] = fr[3]
                if pending:
                    n0 = pending[0]
                    st = st + (("windCaptureSeq", pending[1:], payload, fail_err),)
                    # 已发生失败后，更外层作用域按异常退出(error)拆卸；
                    # 否则它们确实是被捕获带走，记 capture。
                    emit("wind_after", n0[0], "error" if fail_err is not None else "capture")
                    e, env, mode = n0[1], n0[2], "eval"
                elif fail_err is not None:
                    # 拆卸过程中有 after 失败：续延不生成，错误继续按异常
                    # 退出穿过 prompt 之外仍激活的作用域。
                    err, mode = fail_err, "error"
                else:
                    # 全部 capture-after 完成：生成续延，开始 shift body。
                    ptag, pparam, pbody, penv, cont_frames = payload
                    cont = ContVal(ptag, cont_frames)
                    env = (penv, pparam, cont)
                    e, mode = pbody, "eval"
            elif fk == "windErrorCont":
                # error 钩子正常完成：恢复原错误继续向外展开。
                err, mode = fr[1], "error"
            else:  # pragma: no cover
                raise InvalidProgramError(f"内部错误：未知栈帧 {fk!r}")
