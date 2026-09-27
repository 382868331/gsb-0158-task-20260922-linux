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
('shift1', tag, k, body)   一次性 shift：k 只能恢复一次

shift 捕获后 k 被具体化为函数值 ``λx. prompt(t, E_t[x])``，所以
参考机里不存在独立的续延项——续延就是一段可复制的上下文快照。
普通 shift 具体化为可复制快照；shift1 具体化时为每个捕获分配唯一 id，
机器维护已消费 id 集合：第二次调用同一快照在 beta 归约前报
``RefError('consumed')``，对应核心的 ContinuationConsumedError。消费在
实参归约完成后、函数体归约开始前记账，故实参归约失败不消费，函数体内
失败不回滚——与核心同一时序。
"""

from __future__ import annotations

from typing import List, Optional

Term = tuple

REF_STEP_LIMIT = 200_000


class _OnceRegistry:
    """shift1 具体化快照的消费记账（按捕获身份，而非变量名）。"""

    def __init__(self) -> None:
        self._next = 0
        self.consumed: set = set()

    def fresh_id(self) -> int:
        self._next += 1
        return self._next


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
    if t == "lam_once":
        return free_vars(e[2]) - {e[1]}
    if t == "prompt":
        return free_vars(e[2])
    if t == "shift":
        return free_vars(e[3]) - {e[2]}
    if t == "shift1":
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
    if t == "lam_once":
        if e[1] == old:
            return e
        return ("lam_once", e[1], rename(e[2], old, new), e[3])
    if t == "call":
        return ("call", rename(e[1], old, new), rename(e[2], old, new))
    if t == "prompt":
        return ("prompt", e[1], rename(e[2], old, new))
    if t == "shift":
        if e[2] == old:
            return e
        return ("shift", e[1], e[2], rename(e[3], old, new))
    if t == "shift1":
        if e[2] == old:
            return e
        return ("shift1", e[1], e[2], rename(e[3], old, new))
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
    if t in ("lam", "lam_once"):
        x, body = e[1], e[2]
        if x == name:
            return e
        if x in free_vars(repl):
            x2 = gen.fresh(x)
            body = rename(body, x, x2)
            x = x2
        new_body = subst(body, name, repl, gen)
        if t == "lam_once":
            return ("lam_once", x, new_body, e[3])
        return ("lam", x, new_body)
    if t == "call":
        return ("call", subst(e[1], name, repl, gen), subst(e[2], name, repl, gen))
    if t == "prompt":
        return ("prompt", e[1], subst(e[2], name, repl, gen))
    if t in ("shift", "shift1"):
        tag, k, body = e[1], e[2], e[3]
        if k == name:
            return e
        if k in free_vars(repl):
            k2 = gen.fresh(k)
            body = rename(body, k, k2)
            k = k2
        return (t, tag, k, subst(body, name, repl, gen))
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
    raise RefError("type", f"参考机未知帧: {fr!r}")  # pragma: no cover


def plug(frames: List[tuple], term: Term) -> Term:
    """按栈底->栈顶保存的帧把 term 插回上下文。

    最内层帧最先接触洞值，故按**逆序**插接：
    ``E = E_外 ∘ E_内``，plug(E, t) 先应用 E_内。
    """
    for fr in reversed(frames):
        term = _plug_frame(term, fr)
    return term


# --- 小步归约机 -------------------------------------------------------------

def run(program: Term, *, step_limit: int = REF_STEP_LIMIT) -> Term:
    """把程序归约到一个值并返回。

    错误以 ``RefError(kind=...)`` 抛出：
    'uncaught'（shift 无匹配 prompt）、'type'（类型不符）、
    'unbound'（残留自由变量）、'steps'（参考预算耗尽）。
    """
    gen = _Gen()
    once = _OnceRegistry()
    cur: Term = program
    # frames：靠前=靠外（栈底），靠后=靠内（栈顶）。
    frames: List[tuple] = []
    steps = 0

    while True:
        steps += 1
        if steps > step_limit:
            raise RefError("steps", f"参考归约超过 {step_limit} 步")

        t = cur[0]

        # 当前项是值：上交栈顶帧。
        if t in ("int", "lam", "lam_once"):
            if not frames:
                return cur
            fr = frames.pop()
            fk = fr[0]
            if fk == "addL":
                if t != "int":
                    raise RefError("type", f"加法左值不是整数: {cur!r}")
                frames.append(("addR", cur[1]))
                cur = fr[1]
            elif fk == "addR":
                if t != "int":
                    raise RefError("type", f"加法右值不是整数: {cur!r}")
                cur = ("int", fr[1] + cur[1])
            elif fk == "callFn":
                if t not in ("lam", "lam_once"):
                    raise RefError("type", f"被调用者不是函数: {cur!r}")
                frames.append(("callArg", cur))
                cur = fr[1]
            elif fk == "callArg":
                fn = fr[1]
                # callFn 已保证 fn 为 lam/lam_once；具体化的续延也是函数值。
                if fn[0] == "lam_once":
                    # 实参已完成归约：此处记账消费，随后才归约函数体；
                    # 与核心同一时序（实参失败不消费，体内失败不回滚）。
                    oid = fn[3]
                    if oid in once.consumed:
                        raise RefError(
                            "consumed",
                            f"一次性续延已被消费（tag={fn[4]}）",
                        )
                    once.consumed.add(oid)
                cur = subst(fn[2], fn[1], cur, gen)
            elif fk == "prompt":
                pass  # prompt v -> v
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
            frames.append(("prompt", cur[1]))
            cur = cur[2]
        elif t in ("shift", "shift1"):
            tag, kparam, body = cur[1], cur[2], cur[3]
            idx = -1
            for j in range(len(frames) - 1, -1, -1):
                if frames[j][0] == "prompt" and frames[j][1] == tag:
                    idx = j
                    break
            if idx < 0:
                raise RefError("uncaught", f"shift 找不到 tag={tag} 的 prompt")
            captured = tuple(frames[idx + 1 :])
            del frames[idx + 1 :]  # 保留 prompt 帧：body 在其内归约
            x = gen.fresh("x")
            hole = ("prompt", tag, plug(list(captured), ("var", x)))
            if t == "shift1":
                reified = ("lam_once", x, hole, once.fresh_id(), tag)
            else:
                reified = ("lam", x, hole)
            cur = subst(body, kparam, reified, gen)
        else:  # pragma: no cover
            raise RefError("type", f"参考机未知项: {cur!r}")
