"""独立参考实现：显式求值上下文的小步规约（substitution-based small-step）。

本模块是验收用的**独立参考**，刻意不导入、不调用被测核心 ``delimcc``：
  * 语法用自带的不可变元组 s-表达式表示；
  * 归约方式是“显式求值上下文 + 避免捕获替换”：机器维护一个上下文
    帧列表与当前项，每一步把当前项下沉为可约式后收缩一项；
  * shift 的语义靠上下文切分实现——取最近同 tag prompt 帧以内的
    上下文 E_t，把 k 替换为内部续延值 ``kont(t, x, prompt(t, E_t[x]))``；
    调用该值经 beta 归约后，恢复出的上下文自然地被同 tag prompt 重新
    包裹，且续延值（内含 E_t 快照）不可变，故可多次调用。
  * wind 用栈帧 ``windBody`` 标记活动作用域，其插接回快照时重建为完整
    ``wind`` 项——续延恢复重放该上下文时 before/after 自然按动态 wind
    语义重新执行；捕获离开与错误展开则用强制钩子链显式跑 after。

这与被测核心（CEK 环境机器、元组栈帧）是两套独立算法，仅共享题目
语义。参考机自身设步长硬上限，避免异常项导致不终止时排查困难。

元组语法
--------
('int', n) ('var', x) ('add', a, b) ('lam', x, body)
('call', f, a) ('prompt', tag, body) ('shift', tag, k, body)
('wind', before, body, after)
('kont', tag, param, body)  —— 内部值：shift 具体化的续延（非源码语法）
"""

from __future__ import annotations

from typing import List, Optional

Term = tuple

REF_STEP_LIMIT = 200_000


class RefError(Exception):
    """参考归约中的错误；kind 为 'uncaught'/'hook'/'type'/'unbound'/'steps'。"""

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
    if t == "prompt":
        return free_vars(e[2])
    if t == "shift":
        return free_vars(e[3]) - {e[2]}
    if t == "wind":
        # wind 不绑定变量：三个子项同处一个作用域。
        return free_vars(e[1]) | free_vars(e[2]) | free_vars(e[3])
    if t == "kont":
        # 内部续延值：('kont', tag, param, body)，param 在 body 中受限。
        return free_vars(e[3]) - {e[2]}
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
    if t == "kont":
        if e[2] == old:
            return e
        return ("kont", e[1], e[2], rename(e[3], old, new))
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
    if t == "prompt":
        return ("prompt", e[1], subst(e[2], name, repl, gen))
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
    if t == "kont":
        xp = e[2]
        if xp == name:
            return e
        if xp in free_vars(repl):
            xp2 = gen.fresh(xp)
            body2 = rename(e[3], xp, xp2)
            return ("kont", e[1], xp2, subst(body2, name, repl, gen))
        return ("kont", e[1], xp, subst(e[3], name, repl, gen))
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
    if k == "windBody":
        # 重放捕获上下文时，活动 wind 作用域被重建为完整 wind 项：
        # 重入先重跑 before，洞值穿过 body 后再跑 after。
        return ("wind", fr[1], term, fr[2])
    raise RefError("type", f"参考机未知帧: {fr!r}")  # pragma: no cover


def plug(frames: List[tuple], term: Term) -> Term:
    """按栈底->栈顶保存的帧把 term 插回上下文。

    最内层帧最先接触洞值，故按**逆序**插接：
    ``E = E_外 ∘ E_内``，plug(E, t) 先应用 E_内。
    """
    for fr in reversed(frames):
        term = _plug_frame(term, fr)
    return term


def _show(term: Term) -> str:
    """钩子值的可读串（与核心 display 在整数/lambda 情形一致）。"""
    if term[0] == "int":
        return str(term[1])
    if term[0] == "lam":
        return f"<lambda {term[1]}>"
    if term[0] == "kont":
        return f"<continuation tag={term[1]}>"
    return repr(term)  # pragma: no cover


def _fn_param(fn: Term) -> str:
    return fn[1] if fn[0] == "lam" else fn[2]


def _fn_body(fn: Term) -> Term:
    return fn[2] if fn[0] == "lam" else fn[3]


# --- 小步归约机 -------------------------------------------------------------

def run(
    program: Term,
    *,
    step_limit: int = REF_STEP_LIMIT,
    events: Optional[list] = None,
) -> Term:
    """把程序归约到一个值并返回。

    错误以 ``RefError(kind=...)`` 抛出：
    'uncaught'（shift 无匹配 prompt）、'hook'（wind 钩子里捕获/调用
    续延）、'type'（类型不符）、'unbound'（残留自由变量）、
    'steps'（参考预算耗尽，立即抛出不展开钩子）。

    传入 ``events`` 列表时，wind 进入/离开以
    ``("wind_before"/"wind_after", 值展示)`` 追加；错误展开所执行的
    离开钩子同样记录，随后再抛原错误。
    """
    gen = _Gen()
    cur: Term = program
    # frames：靠前=靠外（栈底），靠后=靠内（栈顶）。
    frames: List[tuple] = []
    steps = 0

    def in_hook() -> bool:
        # forceHook：捕获离开/展开/重入链；windBefore/windAfter：正常
        # wind 生命周期里钩子自身正在求值。这些帧之上不允许 shift/调 k。
        return any(
            f[0] in ("forceHook", "windBefore", "windAfter") for f in frames
        )

    def start_unwind(err: RefError) -> Optional[Term]:
        """body 出错时按 wind 语义离开活动作用域。

        返回应继续求值的 after 项；若处于钩子内（钩子自身失败）或没有
        活动 wind 作用域，则直接抛出 err。
        """
        active = [f for f in frames if f[0] == "windBody"]  # 外 -> 内
        if in_hook() or not active:
            raise err
        afters = [f[2] for f in active]  # 外 -> 内
        frames.clear()
        frames.append(("doReraise", err))
        for after in afters:  # 外 -> 内压栈，最内在顶最先执行
            frames.append(("forceHook", "after", after))
        return afters[-1]

    while True:
        steps += 1
        if steps > step_limit:
            raise RefError("steps", f"参考归约超过 {step_limit} 步")

        t = cur[0]

        # 当前项是值：上交栈顶帧。
        if t in ("int", "lam", "kont"):
            if not frames:
                return cur
            fr = frames.pop()
            fk = fr[0]
            if fk == "addL":
                if t != "int":
                    cur = start_unwind(
                        RefError("type", f"加法左值不是整数: {cur!r}")
                    )
                    continue
                frames.append(("addR", cur[1]))
                cur = fr[1]
            elif fk == "addR":
                if t != "int":
                    cur = start_unwind(
                        RefError("type", f"加法右值不是整数: {cur!r}")
                    )
                    continue
                cur = ("int", fr[1] + cur[1])
            elif fk == "callFn":
                if t not in ("lam", "kont"):
                    cur = start_unwind(
                        RefError("type", f"被调用者不是函数: {cur!r}")
                    )
                    continue
                if t == "kont" and in_hook():
                    raise RefError("hook", "wind 钩子里调用了续延")
                frames.append(("callArg", cur))
                cur = fr[1]
            elif fk == "callArg":
                fn = fr[1]
                cur = subst(_fn_body(fn), _fn_param(fn), cur, gen)
            elif fk == "prompt":
                pass  # prompt v -> v
            elif fk == "windBefore":
                # before 完成（值丢弃）：压活动帧，进入 body。
                _, before_term, after, body = fr
                if events is not None:
                    events.append(("wind_before", _show(cur)))
                frames.append(("windBody", before_term, after))
                cur = body
            elif fk == "windBody":
                # body 完成：求 after，暂存 body 值。
                cur_body = cur
                frames.append(("windAfter", cur_body))
                cur = fr[2]
            elif fk == "windAfter":
                # after 完成（值丢弃）：wind 取 body 值。
                if events is not None:
                    events.append(("wind_after", _show(cur)))
                cur = fr[1]
            elif fk == "forceHook":
                # 强制钩子（捕获离开/错误展开）完成：值丢弃，驱动链。
                kind = fr[1]
                if events is not None:
                    events.append(
                        (
                            "wind_before" if kind == "before" else "wind_after",
                            _show(cur),
                        )
                    )
                nxt = frames[-1] if frames else None
                if nxt is not None and nxt[0] == "forceHook":
                    cur = nxt[2]
                elif nxt is not None and nxt[0] == "doShift":
                    frames.pop()
                    cur = nxt[1]
                elif nxt is not None and nxt[0] == "doReraise":
                    raise nxt[1]
                else:  # pragma: no cover
                    raise RefError("type", f"参考机未知钩子收尾帧: {nxt!r}")
            else:  # pragma: no cover
                raise RefError("type", f"参考机未知帧: {fk!r}")
            continue

        if t == "var":
            cur = start_unwind(
                RefError("unbound", f"残留自由变量（未绑定）: {cur[1]!r}")
            )
            continue
        if t == "add":
            frames.append(("addL", cur[2]))
            cur = cur[1]
        elif t == "call":
            frames.append(("callFn", cur[2]))
            cur = cur[1]
        elif t == "prompt":
            frames.append(("prompt", cur[1]))
            cur = cur[2]
        elif t == "wind":
            before, body, after = cur[1], cur[2], cur[3]
            # 先求 before；帧保存 before 项（快照重放重建 wind 用）、
            # after 与 body。
            frames.append(("windBefore", before, after, body))
            cur = before
        elif t == "shift":
            tag, kparam, body = cur[1], cur[2], cur[3]
            if in_hook():
                raise RefError("hook", f"wind 钩子里 shift(tag={tag})")
            idx = -1
            for j in range(len(frames) - 1, -1, -1):
                if frames[j][0] == "prompt" and frames[j][1] == tag:
                    idx = j
                    break
            if idx < 0:
                cur = start_unwind(
                    RefError("uncaught", f"shift 找不到 tag={tag} 的 prompt")
                )
                continue
            captured = tuple(frames[idx + 1 :])
            del frames[idx + 1 :]  # 保留 prompt 帧：body 在其内归约
            x = gen.fresh("x")
            reified = (
                "kont",
                tag,
                x,
                ("prompt", tag, plug(list(captured), ("var", x))),
            )
            newbody = subst(body, kparam, reified, gen)
            # 捕获离开：活动 wind 作用域由内向外执行 after。
            exits = [f[2] for f in captured if f[0] == "windBody"]  # 外 -> 内
            if exits:
                frames.append(("doShift", newbody))
                for after in exits:  # 外 -> 内压栈，最内在顶最先执行
                    frames.append(("forceHook", "after", after))
                cur = exits[-1]
            else:
                cur = newbody
        else:  # pragma: no cover
            raise RefError("type", f"参考机未知项: {cur!r}")
