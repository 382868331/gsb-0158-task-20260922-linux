"""独立参考实现：显式求值上下文的小步规约（substitution-based small-step）。

本模块是验收用的**独立参考**，刻意不导入、不调用被测核心 ``delimcc``：
  * 语法用自带的不可变元组 s-表达式表示；
  * 归约方式是“显式求值上下文 + 避免捕获替换”：机器维护一个上下文
    帧列表与当前项，每一步把当前项下沉为可约式后收缩一项；
  * shift 的语义靠上下文切分实现——取最近同 tag prompt 帧以内的
    上下文 E_t，把 k 替换为函数值 ``λx. prompt(t, E_t[x])``；调用该
    值经 beta 归约后，恢复出的上下文自然地被同 tag prompt 重新包裹，
    且函数值（内含 E_t 快照）不可变，故可多次调用。

这与被测核心（CEK 环境机器、元组栈帧）是两套独立算法，仅共享题目
语义。参考机自身设步长硬上限，避免异常项导致不终止时排查困难。

元组语法
--------
('int', n) ('var', x) ('add', a, b) ('lam', x, body)
('call', f, a) ('prompt', tag, body) ('shift', tag, k, body)
('wind', before, body, after)

shift 捕获后 k 被具体化为普通函数值 ``λx. prompt(t, E_t[x])``，所以
参考机里不存在独立的续延项——续延就是一段可复制的上下文快照。

Wind（dynamic-wind 作用域钩子）同样以显式上下文帧实现，与被测 CEK
机器相互独立：捕获跨过激活作用域时先自内向外执行 after，具体化的
续延在恢复时自外向内执行 before，值穿过边界再执行 after。
``run_trace`` 额外返回事件序列（事件名/参数与被测机一致），用于
交叉核对钩子顺序；``run`` 仍只返回最终值项。
"""

from __future__ import annotations

from typing import List, Optional

Term = tuple

REF_STEP_LIMIT = 200_000


class RefError(Exception):
    """参考归约中的错误；kind 为 'uncaught'/'type'/'unbound'/'steps'。"""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


# --- 自由变量与避免捕获替换 -------------------------------------------------

def free_vars(e: Term) -> set:
    """计算项中自由变量集合（显式递归处理绑定器 lam/shift）。"""
    t = e[0]
    if t == "int":
        return set()
    if t == "var":
        return {e[1]}
    if t == "add" or t == "call":
        return free_vars(e[1]) | free_vars(e[2])
    if t == "lam":
        return free_vars(e[2]) - {e[1]}
    if t == "prompt" or t == "promptrw":
        return free_vars(e[2])
    if t == "shift":
        return free_vars(e[3]) - {e[2]}
    if t == "wind":
        return free_vars(e[1]) | free_vars(e[2]) | free_vars(e[3])
    if t == "windresume":
        # ('windresume', sid, before, body, after)：无新绑定。
        return free_vars(e[2]) | free_vars(e[3]) | free_vars(e[4])
    raise RefError("type", f"参考机未知项: {e!r}")  # pragma: no cover


class _Gen:
    def __init__(self) -> None:
        self.n = 0

    def fresh(self, base: str) -> str:
        self.n += 1
        return f"{base}__{self.n}"


def rename(e: Term, old: str, new: str) -> Term:
    """把自由出现的 old 改名为 new（调用方保证 new 不在 e 中受限）。"""
    t = e[0]
    if t == "int":
        return e
    if t == "var":
        return ("var", new) if e[1] == old else e
    if t == "add":
        return ("add", rename(e[1], old, new), rename(e[2], old, new))
    if t == "lam":
        if e[1] == old:
            return e
        return ("lam", e[1], rename(e[2], old, new))
    if t == "call":
        return ("call", rename(e[1], old, new), rename(e[2], old, new))
    if t == "prompt":
        return ("prompt", e[1], rename(e[2], old, new))
    if t == "promptrw":
        return ("promptrw", e[1], rename(e[2], old, new), e[3])
    if t == "shift":
        if e[2] == old:
            return e
        return ("shift", e[1], e[2], rename(e[3], old, new))
    if t == "wind":
        return (
            "wind",
            rename(e[1], old, new),
            rename(e[2], old, new),
            rename(e[3], old, new),
        )
    if t == "windresume":
        return (
            "windresume",
            e[1],
            rename(e[2], old, new),
            rename(e[3], old, new),
            rename(e[4], old, new),
        )
    raise RefError("type", f"参考机未知项: {e!r}")  # pragma: no cover


def subst(e: Term, name: str, repl: Term, gen: _Gen) -> Term:
    """e[name := repl]，避免捕获（必要时对绑定变量 alpha 改名）。"""
    t = e[0]
    if t == "int":
        return e
    if t == "var":
        return repl if e[1] == name else e
    if t == "add":
        return ("add", subst(e[1], name, repl, gen), subst(e[2], name, repl, gen))
    if t == "lam":
        x, body = e[1], e[2]
        if x == name:
            return e
        if x in free_vars(repl):
            x2 = gen.fresh(x)
            body = rename(body, x, x2)
            x = x2
        return ("lam", x, subst(body, name, repl, gen))
    if t == "call":
        return ("call", subst(e[1], name, repl, gen), subst(e[2], name, repl, gen))
    if t in ("prompt",):
        return (t, e[1], subst(e[2], name, repl, gen))
    if t == "promptrw":
        # ('promptrw', tag, body, 捕获帧数)：第 4 字段为元数据，原样保留。
        return ("promptrw", e[1], subst(e[2], name, repl, gen), e[3])
    if t == "shift":
        tag, k, body = e[1], e[2], e[3]
        if k == name:
            return e
        if k in free_vars(repl):
            k2 = gen.fresh(k)
            body = rename(body, k, k2)
            k = k2
        return ("shift", tag, k, subst(body, name, repl, gen))
    if t == "wind":
        return (
            "wind",
            subst(e[1], name, repl, gen),
            subst(e[2], name, repl, gen),
            subst(e[3], name, repl, gen),
        )
    if t == "windresume":
        return (
            "windresume",
            e[1],
            subst(e[2], name, repl, gen),
            subst(e[3], name, repl, gen),
            subst(e[4], name, repl, gen),
        )
    raise RefError("type", f"参考机未知项: {e!r}")  # pragma: no cover


# --- 上下文插接 -------------------------------------------------------------

def _plug_frame(term: Term, fr: tuple) -> Term:
    k = fr[0]
    if k == "addL":
        return ("add", term, fr[1])
    if k == "addR":
        return ("add", ("int", fr[1]), term)
    if k == "callFn":
        return ("call", term, fr[1])
    if k == "callArg":
        return ("call", fr[1], term)
    if k == "prompt":
        return ("prompt", fr[1], term)
    if k == "windBoundary":
        # 捕获快照中的作用域边界：恢复时重包为 windresume 节点。
        # fr = ("windBoundary", sid, before, after)
        return ("windresume", fr[1], fr[2], term, fr[3])
    raise RefError("type", f"参考机未知帧: {fr!r}")  # pragma: no cover


def plug(frames: List[tuple], term: Term) -> Term:
    """按栈底->栈顶保存的帧把 term 插回上下文。

    最内层帧最先接触洞值，故按**逆序**插接：
    ``E = E_外 ∘ E_内``，plug(E, t) 先应用 E_内。
    """
    for fr in reversed(frames):
        term = _plug_frame(term, fr)
    return term


def _display(t: Term) -> str:
    if t[0] == "int":
        return str(t[1])
    if t[0] == "lam":
        return f"<lambda {t[1]}>"
    return repr(t)  # pragma: no cover


# --- 小步归约机 -------------------------------------------------------------

def run_trace(program: Term, *, step_limit: int = REF_STEP_LIMIT):
    """把程序归约到一个值，返回 ``(值项, 事件元组列表)``。

    事件与被测 CEK 机器同名同参数（仅限成功路径：prompt 的 src/rewrap、
    shift_capture/cont_call、wind_before/wind_after、add、result）。
    错误以 ``RefError(kind=...)`` 抛出，错误路径的钩子展开不在参考机
    建模（由核心的手写测试按手算期望核对）。
    """
    gen = _Gen()
    cur: Term = program
    # frames：靠前=靠外（栈底），靠后=靠内（栈顶）。
    frames: List[tuple] = []
    events: List[tuple] = []
    sid_ctr = 0
    steps = 0

    def wind_value_return(t: Term, fr: tuple) -> None:
        """处理 wind 相关的返回帧；返回 True 表示已处理。"""
        nonlocal cur
        fk = fr[0]
        if fk == "windBefore":
            _, before, body, after, sid = fr
            frames.append(("windActive", before, after, sid))
            cur = body
            return True
        if fk == "windActive":
            _, before, after, sid = fr
            bodyval = t
            events.append(("wind_after", sid, "normal"))
            frames.append(("windPass", bodyval))
            cur = after
            return True
        if fk == "windPass":
            cur = fr[1]
            return True
        if fk == "rwBefore":
            # fr = ("rwBefore", before, inner, after, sid)
            _, before, inner, after, sid = fr
            frames.append(("rwInner", before, after, sid))
            cur = inner
            return True
        if fk == "rwInner":
            # fr = ("rwInner", before, after, sid)
            _, before, after, sid = fr
            events.append(("wind_after", sid, "normal"))
            frames.append(("rwPass", t))
            cur = after
            return True
        if fk == "rwPass":
            cur = fr[1]
            return True
        if fk == "windCap":
            _, remaining, tag, kparam, body, reified = fr
            if remaining:
                sid2, after2 = remaining[0]
                events.append(("wind_after", sid2, "capture"))
                frames.append(
                    ("windCap", remaining[1:], tag, kparam, body, reified)
                )
                cur = after2
            else:
                cur = subst(body, kparam, reified, gen)
            return True
        if fk == "promptrw":
            events.append(("prompt_rewrap_exit", fr[1], _display(t)))
            cur = t
            return True
        return False

    while True:
        steps += 1
        if steps > step_limit:
            raise RefError("steps", f"参考归约超过 {step_limit} 步")

        t = cur[0]

        # 当前项是值：上交栈顶帧。
        if t in ("int", "lam"):
            if not frames:
                events.append(("result", _display(cur)))
                return cur, events
            fr = frames.pop()
            fk = fr[0]
            if wind_value_return(cur, fr):
                continue
            if fk == "addL":
                if t != "int":
                    raise RefError("type", f"加法左值不是整数: {cur!r}")
                frames.append(("addR", cur[1]))
                cur = fr[1]
            elif fk == "addR":
                if t != "int":
                    raise RefError("type", f"加法右值不是整数: {cur!r}")
                s = fr[1] + cur[1]
                events.append(("add", fr[1], cur[1], s))
                cur = ("int", s)
            elif fk == "callFn":
                if t != "lam":
                    raise RefError("type", f"被调用者不是函数: {cur!r}")
                frames.append(("callArg", cur))
                cur = fr[1]
            elif fk == "callArg":
                fn = fr[1]
                # callFn 已保证 fn 为 lam；具体化的续延也是 lam。
                cur = subst(fn[2], fn[1], cur, gen)
            elif fk == "prompt":
                events.append(("prompt_exit", fr[1], _display(cur)))
                # prompt v -> v
            else:  # pragma: no cover
                raise RefError("type", f"参考机未知帧: {fk!r}")
            continue

        if t == "var":
            raise RefError("unbound", f"残留自由变量（未绑定）: {cur[1]!r}")
        if t == "add":
            frames.append(("addL", cur[2]))
            cur = cur[1]
        elif t == "call":
            frames.append(("callFn", cur[2]))
            cur = cur[1]
        elif t == "prompt":
            events.append(("prompt_enter", cur[1]))
            frames.append(("prompt", cur[1]))
            cur = cur[2]
        elif t == "promptrw":
            # ('promptrw', tag, body, 捕获帧数)：具体化续延被调用后进入。
            events.append(("cont_call", cur[1], cur[3]))
            events.append(("prompt_rewrap_enter", cur[1]))
            frames.append(("promptrw", cur[1]))
            cur = cur[2]
        elif t == "wind":
            _, before, body, after = cur
            sid = sid_ctr
            sid_ctr += 1
            events.append(("wind_before", sid, "enter"))
            frames.append(("windBefore", before, body, after, sid))
            cur = before
        elif t == "windresume":
            _, sid, before, inner, after = cur
            events.append(("wind_before", sid, "resume"))
            frames.append(("rwBefore", before, inner, after, sid))
            cur = before
        elif t == "shift":
            tag, kparam, body = cur[1], cur[2], cur[3]
            idx = -1
            for j in range(len(frames) - 1, -1, -1):
                if frames[j][0] in ("prompt", "promptrw") and frames[j][1] == tag:
                    idx = j
                    break
            if idx < 0:
                raise RefError("uncaught", f"shift 找不到 tag={tag} 的 prompt")
            captured = list(frames[idx + 1 :])
            del frames[idx + 1 :]  # 保留 prompt 帧：body 在其内归约
            # 捕获区内激活的 wind 作用域（外->内）：源码 wind 与恢复中的
            # windresume 内域都算激活作用域。
            scopes = []
            for f in captured:
                if f[0] == "windActive":  # ("windActive", before, after, sid)
                    scopes.append((f[3], f[1], f[2]))
                elif f[0] == "rwInner":  # ("rwInner", before, after, sid)
                    scopes.append((f[3], f[1], f[2]))
            def mark(f):
                if f[0] == "windActive":  # ("windActive", before, after, sid)
                    return ("windBoundary", f[3], f[1], f[2])
                if f[0] == "rwInner":  # ("rwInner", before, after, sid)
                    return ("windBoundary", f[3], f[1], f[2])
                if f[0] == "promptrw":
                    # 恢复期间再次被捕获的重包 prompt：快照里归一化为普通
                    # prompt（plug 重建即定义式 prompt），与核心一致。
                    return ("prompt", f[1])
                return f

            captured_marked = [mark(f) for f in captured]
            events.append(("shift_capture", tag, len(captured)))
            x = gen.fresh("x")
            reified = (
                "lam",
                x,
                ("promptrw", tag, plug(captured_marked, ("var", x)), len(captured)),
            )
            if scopes:
                # 自内向外执行 capture-after，再开始 shift body。
                jobs = [(sid, afterx) for sid, _, afterx in reversed(scopes)]
                sid0, after0 = jobs[0]
                events.append(("wind_after", sid0, "capture"))
                frames.append(
                    ("windCap", jobs[1:], tag, kparam, body, reified)
                )
                cur = after0
            else:
                cur = subst(body, kparam, reified, gen)
        else:  # pragma: no cover
            raise RefError("type", f"参考机未知项: {cur!r}")


def run(program: Term, *, step_limit: int = REF_STEP_LIMIT) -> Term:
    """把程序归约到一个值并返回（事件轨迹丢弃，见 :func:`run_trace`）。"""
    term, _ = run_trace(program, step_limit=step_limit)
    return term
