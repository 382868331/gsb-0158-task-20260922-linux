"""Independent reference semantics: explicit-context small-step reduction.

This module is a **separate implementation** of the same language, used as
an oracle in the tests.  It never imports or calls :mod:`delimcc.core`; it
does not even share its runtime values.  Expected results are obtained
here from first principles, then compared against the CEK core.

Presentation
------------
A state is a plain *term*.  Every reduction step decomposes the term into
an evaluation context ``E`` and a redex, rewrites the redex, and plugs the
contractum back:  ``t = E[r]  ->  E[r']``.

Evaluation contexts (call-by-value, left-to-right)::

    E ::= []
        | E + e2 | v1 + E
        | E(e2) | v1(E)
        | prompt tag { E }

Reduction rules (values: integers, lambdas, reified continuations)::

    n1 + n2                         -> n1 + n2        (integer sum)
    (lam x. b) v                    -> b[x := v]      (capture avoiding)
    prompt t { v }                  -> v
    K_tag(F) v                      -> prompt t { F[v] }
    E[prompt t { F[shift t k. b] }] -> E[prompt t { b[k := K_tag(F)] }]

``F`` is the context between the ``shift`` and the nearest enclosing
``prompt t`` (it contains no inner prompt tagged ``t``).  Multi-shot use
of ``k`` falls out of ordinary substitution: substituting ``K_tag(F)``
copies the term ``F`` at every use site.  When decomposition reaches a
``shift`` with no matching prompt frame anywhere in ``E``, the term is
proven stuck and :class:`NoPromptError` is raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from . import ast as A
from .errors import BadAstError, NoPromptError, StepLimitError, UnboundVarError

DEFAULT_REF_MAX_STEPS = 20000


# --------------------------------------------------------------------------- #
# Reference term layer (independent of core runtime values)                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RInt:
    value: int


@dataclass(frozen=True)
class RAdd:
    left: "RTerm"
    right: "RTerm"


@dataclass(frozen=True)
class RVar:
    name: str


@dataclass(frozen=True)
class RLam:
    param: str
    body: "RTerm"


@dataclass(frozen=True)
class RCall:
    fn: "RTerm"
    arg: "RTerm"


@dataclass(frozen=True)
class RPrompt:
    tag: int
    body: "RTerm"


@dataclass(frozen=True)
class RShift:
    tag: int
    param: str
    body: "RTerm"


@dataclass(frozen=True)
class RCont:
    """Reified continuation: an explicit captured context ``frames``."""

    tag: int
    frames: tuple[tuple, ...]


RTerm = Union[RInt, RAdd, RVar, RLam, RCall, RPrompt, RShift, RCont]
RValue = Union[RInt, RLam, RCont]

# Context frames, stored outer->inner:
#   ("addR", right_term)     hole is the left operand
#   ("addL", left_RInt)      hole is the right operand
#   ("callArg", arg_term)    hole is the callee
#   ("callFn", callee_value) hole is the argument
#   ("prompt", tag)          hole is the prompt body


def from_ast(node: A.Node) -> RTerm:
    """Translate an (already validated) AST into the reference term layer."""
    if isinstance(node, A.Int):
        return RInt(node.value)
    if isinstance(node, A.Add):
        return RAdd(from_ast(node.left), from_ast(node.right))
    if isinstance(node, A.Var):
        return RVar(node.name)
    if isinstance(node, A.Lam):
        return RLam(node.param, from_ast(node.body))
    if isinstance(node, A.Call):
        return RCall(from_ast(node.fn), from_ast(node.arg))
    if isinstance(node, A.Prompt):
        return RPrompt(node.tag, from_ast(node.body))
    if isinstance(node, A.Shift):
        return RShift(node.tag, node.param, from_ast(node.body))
    raise BadAstError(f"reference cannot translate {type(node).__name__}")


# --------------------------------------------------------------------------- #
# Explicit contexts                                                            #
# --------------------------------------------------------------------------- #


def plug(frames: list[tuple] | tuple[tuple, ...], term: RTerm) -> RTerm:
    """Plug ``term`` into the hole of the context represented by frames."""
    for fr in reversed(frames):
        kind = fr[0]
        if kind == "addR":
            term = RAdd(term, fr[1])
        elif kind == "addL":
            term = RAdd(fr[1], term)
        elif kind == "callArg":
            term = RCall(term, fr[1])
        elif kind == "callFn":
            term = RCall(fr[1], term)
        elif kind == "prompt":
            term = RPrompt(fr[1], term)
        else:  # pragma: no cover
            raise BadAstError(f"reference: corrupt context frame {fr!r}")
    return term


def _is_value(t: RTerm) -> Optional[RValue]:
    if isinstance(t, (RInt, RLam, RCont)):
        return t
    return None


# --------------------------------------------------------------------------- #
# Capture-avoiding substitution                                                #
# --------------------------------------------------------------------------- #


def free_vars(t: RTerm, bound: frozenset[str] = frozenset()) -> set[str]:
    if isinstance(t, RVar):
        return set() if t.name in bound else {t.name}
    if isinstance(t, RInt):
        return set()
    if isinstance(t, RCont):
        out: set[str] = set()
        for fr in t.frames:
            for sub in fr[1:]:
                if isinstance(sub, RTerm):
                    out |= free_vars(sub, bound)
                # frame[1] of addL is an int marker, not a term
        return out
    if isinstance(t, (RLam,)):
        return free_vars(t.body, bound | {t.param})
    if isinstance(t, RShift):
        return free_vars(t.body, bound | {t.param})
    if isinstance(t, RAdd):
        return free_vars(t.left, bound) | free_vars(t.right, bound)
    if isinstance(t, RCall):
        return free_vars(t.fn, bound) | free_vars(t.arg, bound)
    if isinstance(t, RPrompt):
        return free_vars(t.body, bound)
    raise BadAstError(f"reference: free_vars on {type(t).__name__}")  # pragma: no cover


_fresh_counter = 0


def _fresh(base: str) -> str:
    global _fresh_counter
    _fresh_counter += 1
    return f"{base}__{_fresh_counter}"


def _rename(t: RTerm, old: str, new: str) -> RTerm:
    if isinstance(t, RVar):
        return RVar(new) if t.name == old else t
    if isinstance(t, RInt):
        return t
    if isinstance(t, RCont):
        return RCont(
            t.tag,
            tuple(
                tuple(_rename(x, old, new) if isinstance(x, RTerm) else x for x in fr)
                for fr in t.frames
            ),
        )
    if isinstance(t, RLam):
        if t.param == old:
            return t
        if t.param == new:
            return t
        return RLam(t.param, _rename(t.body, old, new))
    if isinstance(t, RShift):
        if t.param in (old, new):
            return t
        return RShift(t.tag, t.param, _rename(t.body, old, new))
    if isinstance(t, RAdd):
        return RAdd(_rename(t.left, old, new), _rename(t.right, old, new))
    if isinstance(t, RCall):
        return RCall(_rename(t.fn, old, new), _rename(t.arg, old, new))
    if isinstance(t, RPrompt):
        return RPrompt(t.tag, _rename(t.body, old, new))
    raise BadAstError(f"reference: rename on {type(t).__name__}")  # pragma: no cover


def subst(t: RTerm, name: str, v: RTerm) -> RTerm:
    """Capture-avoiding substitution ``t[name := v]``."""
    if isinstance(t, RVar):
        return v if t.name == name else t
    if isinstance(t, (RInt, RCont)):
        if isinstance(t, RCont):
            return RCont(
                t.tag,
                tuple(
                    tuple(subst(x, name, v) if isinstance(x, RTerm) else x for x in fr)
                    for fr in t.frames
                ),
            )
        return t
    if isinstance(t, RLam):
        if t.param == name:
            return t
        if t.param in free_vars(v):
            new_p = _fresh(t.param)
            return RLam(new_p, subst(_rename(t.body, t.param, new_p), name, v))
        return RLam(t.param, subst(t.body, name, v))
    if isinstance(t, RShift):
        if t.param == name:
            return t
        if t.param in free_vars(v):
            new_p = _fresh(t.param)
            return RShift(
                t.tag, new_p, subst(_rename(t.body, t.param, new_p), name, v)
            )
        return RShift(t.tag, t.param, subst(t.body, name, v))
    if isinstance(t, RAdd):
        return RAdd(subst(t.left, name, v), subst(t.right, name, v))
    if isinstance(t, RCall):
        return RCall(subst(t.fn, name, v), subst(t.arg, name, v))
    if isinstance(t, RPrompt):
        return RPrompt(t.tag, subst(t.body, name, v))
    raise BadAstError(f"reference: subst on {type(t).__name__}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Decomposition + one reduction                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RefResult:
    """Outcome of the reference reducer."""

    value: RValue
    steps: int
    events: tuple[tuple[str, int], ...]  # (kind, tag) per reduction

    def descriptor(self) -> tuple:
        """A structural, comparable description of the final value."""
        return value_descriptor(self.value)


def value_descriptor(v: RValue) -> tuple:
    if isinstance(v, RInt):
        return ("int", v.value)
    if isinstance(v, RLam):
        return ("closure", v.param)
    return ("continuation", v.tag, len(v.frames))


def _decompose(t: RTerm) -> tuple[list[tuple], RTerm]:
    """Find the redex: return (outer->inner context frames, redex term)."""
    frames: list[tuple] = []
    while True:
        if isinstance(t, RAdd):
            lv = _is_value(t.left)
            if lv is None:
                frames.append(("addR", t.right))
                t = t.left
                continue
            rv = _is_value(t.right)
            if rv is None:
                frames.append(("addL", t.left))
                t = t.right
                continue
            return frames, t
        if isinstance(t, RCall):
            if _is_value(t.fn) is None:
                frames.append(("callArg", t.arg))
                t = t.fn
                continue
            if _is_value(t.arg) is None:
                frames.append(("callFn", t.fn))
                t = t.arg
                continue
            return frames, t
        if isinstance(t, RPrompt):
            if _is_value(t.body) is None:
                frames.append(("prompt", t.tag))
                t = t.body
                continue
            return frames, t
        if isinstance(t, RShift):
            return frames, t
        # a value with no context: program finished
        return [], t


def reduce_once(
    frames: list[tuple], redex: RTerm, events: list[tuple[str, int]]
) -> RTerm:
    """Apply one reduction rule; return the COMPLETE next term.

    ``frames`` is the outer->inner context around ``redex``.  Most rules
    contract the redex and plug the contractum back into the whole
    context; the shift rule splices the context itself, so it plugs only
    the frames *below* the matched prompt.
    """
    if isinstance(redex, RAdd):
        if not isinstance(redex.left, RInt) or not isinstance(redex.right, RInt):
            raise BadAstError(
                f"reference: + requires two integers, got "
                f"{type(redex.left).__name__} + {type(redex.right).__name__}"
            )
        return plug(frames, RInt(redex.left.value + redex.right.value))

    if isinstance(redex, RCall):
        callee, arg = redex.fn, redex.arg
        if isinstance(callee, RLam):
            return plug(frames, subst(callee.body, callee.param, arg))
        if isinstance(callee, RCont):
            events.append(("resume", callee.tag))
            # K_tag(F) v  ->  prompt tag { F[v] }
            return plug(frames, RPrompt(callee.tag, plug(callee.frames, arg)))
        raise BadAstError(
            f"reference: cannot call non-function {type(callee).__name__}"
        )

    if isinstance(redex, RPrompt):
        events.append(("prompt-exit", redex.tag))
        return plug(frames, redex.body)  # body is a value

    if isinstance(redex, RShift):
        tag = redex.tag
        # nearest enclosing same-tag prompt, scanning inner -> outer
        for i in range(len(frames) - 1, -1, -1):
            if frames[i][0] == "prompt" and frames[i][1] == tag:
                below = frames[:i]
                captured = tuple(frames[i + 1 :])
                events.append(("shift", tag))
                new_body = subst(redex.body, redex.param, RCont(tag, captured))
                # E[prompt t { F[shift t k. b] }]
                #   -> E[prompt t { b[k := K_t(F)] }]
                return plug(below, RPrompt(tag, new_body))
        raise NoPromptError(
            f"reference: shift(tag={tag}) has no enclosing prompt tagged {tag}"
        )

    if isinstance(redex, RVar):
        raise UnboundVarError(f"reference: unbound variable {redex.name!r}")

    raise BadAstError(
        f"reference: stuck non-reducible term {type(redex).__name__}"
    )


def reduce_program(
    program: A.Node, *, max_steps: int = DEFAULT_REF_MAX_STEPS,
    max_nodes: int = A.DEFAULT_MAX_NODES,
) -> RefResult:
    """Reduce an AST to a value using explicit-context small steps."""
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise BadAstError("max_steps must be a positive int (bool rejected)")
    A.check_program(program, max_nodes)
    t = from_ast(program)
    events: list[tuple[str, int]] = []
    steps = 0
    while True:
        frames, redex = _decompose(t)
        if not frames and _is_value(redex) is not None:
            return RefResult(redex, steps, tuple(events))  # type: ignore[arg-type]
        steps += 1
        if steps > max_steps:
            raise StepLimitError(
                f"reference: exceeded {max_steps} reduction steps (budget)"
            )
        t = reduce_once(frames, redex, events)
