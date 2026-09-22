"""Core interpreter: a CEK machine with multiple integer-tagged prompts.

Semantics (call-by-value, left-to-right)
----------------------------------------
* ``Prompt(tag, body)`` pushes a prompt frame carrying ``tag`` and evaluates
  ``body``; a value returning through the frame simply removes it.
* ``Shift(tag, k, body)`` searches the continuation stack **top-down** for
  the nearest prompt frame with the same tag.  The frames above it are
  captured (reified as a continuation value), the matched prompt frame and
  the captured frames are removed, and ``body`` is evaluated *inside* a
  fresh prompt with that tag (so further same-tag shifts inside ``body``
  see that prompt).  If no matching prompt exists, :class:`NoPromptError`
  is raised -- a proven failure, distinct from step-budget exhaustion.
* Invoking a captured continuation ``k(v)`` pushes a fresh prompt frame
  with the same tag and replays the captured frames with ``v`` fed into
  the innermost one.  Captured frames are copied, not moved, so ``k`` can
  be invoked any number of times.

The machine performs one explicit transition per *step*; the interface
budget (default 20000 steps) raises :class:`StepLimitError`.

There is no mutable state: environments are lexical parent chains and
values are frozen.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

from . import ast as A
from .errors import (
    BadAstError,
    NoPromptError,
    StepLimitError,
    UnboundVarError,
)

#: Interface boundary: maximum small-step transitions per evaluation.
DEFAULT_MAX_STEPS = 20000


# --------------------------------------------------------------------------- #
# Runtime values and environment                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntVal:
    value: int


@dataclass(frozen=True)
class Env:
    """One-binding extension of a lexical environment (immutable chain)."""

    name: str
    val: "Value"
    parent: Optional["Env"] = None

    def lookup(self, name: str, where: str) -> "Value":
        env: Optional[Env] = self
        while env is not None:
            if env.name == name:
                return env.val
            env = env.parent
        raise _Unbound(name, where)


def _bind(parent: Optional[Env], name: str, val: "Value") -> Env:
    return Env(name, val, parent)


@dataclass(frozen=True)
class Closure:
    param: str
    body: A.Node
    env: Optional[Env]


@dataclass(frozen=True)
class ContVal:
    """A reified continuation captured by ``shift``.

    ``frames`` is a tuple of CEK frames stored bottom-to-top (the order in
    which they must be pushed back).  ``tag`` is the matched prompt tag.
    """

    tag: int
    frames: tuple[tuple, ...]
    origin: str  # human-readable location of the capturing Shift node


Value = IntVal | Closure | ContVal


class _Unbound(Exception):
    """Internal signal carrying an unbound name out of ``Env.lookup``."""

    def __init__(self, name: str, where: str) -> None:
        self.name = name
        self.where = where


# --------------------------------------------------------------------------- #
# Events and result                                                            #
# --------------------------------------------------------------------------- #

# Event kinds:
#   "prompt-enter" -- evaluation entered a prompt body
#   "prompt-exit"  -- a value returned normally through a prompt frame
#   "shift"        -- a shift captured and removed a prompt
#   "resume"       -- a captured continuation was invoked


@dataclass(frozen=True)
class Event:
    kind: str
    tag: int
    step: int


@dataclass(frozen=True)
class Result:
    value: Value
    events: tuple[Event, ...]
    steps: int
    node_count: int

    def as_int(self) -> int:
        """Return the integer payload, or raise if the result is not an int."""
        if not isinstance(self.value, IntVal):
            raise BadAstError(
                f"result is {describe_value(self.value)}, not an integer"
            )
        return self.value.value


def describe_value(v: Value) -> str:
    """Stable, human-readable rendering of a runtime value."""
    if isinstance(v, IntVal):
        return f"int({v.value})"
    if isinstance(v, Closure):
        return f"<closure param={v.param!r}>"
    if isinstance(v, ContVal):
        return f"<continuation tag={v.tag} frames={len(v.frames)}>"
    raise TypeError(f"not a runtime value: {v!r}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# CEK machine                                                                  #
# --------------------------------------------------------------------------- #

# Frame tuples (immutable):
#   ("addR", right_expr, env)  -- left side done, evaluate right
#   ("addL", IntVal)           -- right side done, perform the add
#   ("callArg", arg_expr, env) -- callee done, evaluate argument
#   ("callFn", callee_value)   -- argument done, enter the call
#   ("prompt", tag)            -- prompt boundary


def _node_loc(node: Optional[A.Node]) -> str:
    if node is None:
        return "runtime"
    if isinstance(node, (A.Prompt, A.Shift)):
        return f"{type(node).__name__}(tag={node.tag})"
    if isinstance(node, A.Int):
        return f"Int(value={node.value!r})"
    if isinstance(node, A.Var):
        return f"Var(name={node.name!r})"
    return type(node).__name__


def _capture_shift(
    e: A.Shift,
    k: list[tuple],
    events: list[Event],
    steps: int,
) -> ContVal:
    """Splice the stack up to the nearest same-tag prompt; return the cont.

    Frames above the matched prompt are captured bottom-to-top; they and
    the matched prompt frame are removed from ``k``.  Raises
    :class:`NoPromptError` when no same-tag prompt exists -- a proven
    failure, never budget exhaustion.
    """
    for i in range(len(k) - 1, -1, -1):
        if k[i][0] == "prompt" and k[i][1] == e.tag:
            captured = tuple(k[i + 1 :])
            del k[i:]  # captured frames AND the matched prompt frame
            events.append(Event("shift", e.tag, steps))
            return ContVal(e.tag, captured, _node_loc(e))
    raise NoPromptError(
        f"shift(tag={e.tag}) found no enclosing prompt with tag {e.tag} "
        "in the current continuation",
        loc=("program", _node_loc(e)),
    )


def _type_error(message: str, node: Optional[A.Node]) -> BadAstError:
    return BadAstError(message, loc=("program", _node_loc(node)))


def run(
    program: A.Node,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_nodes: int = A.DEFAULT_MAX_NODES,
    trace: bool = False,
) -> Result:
    """Evaluate ``program``; return a :class:`Result` or raise a package error.

    ``trace=True`` prints one line per machine transition to stderr.
    Prompt/shift/resume events are collected into the result regardless.
    """
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise BadAstError("max_steps must be a positive int (bool rejected)")
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes <= 0:
        raise BadAstError("max_nodes must be a positive int (bool rejected)")

    node_count = A.check_program(program, max_nodes)

    k: list[tuple] = []
    env: Optional[Env] = None
    expr: Optional[A.Node] = program
    val: Optional[Value] = None
    events: list[Event] = []
    steps = 0

    while True:
        steps += 1
        if steps > max_steps:
            raise StepLimitError(
                f"evaluation exceeded {max_steps} steps (budget exhausted; "
                "this is not a proof that no result exists)"
            )

        # ---------------- eval phase ----------------
        if expr is not None:
            e = expr
            if trace:
                print(f"step {steps}: eval {_node_loc(e)}", file=sys.stderr)

            if isinstance(e, A.Int):
                val, expr = IntVal(e.value), None
            elif isinstance(e, A.Var):
                if env is None:
                    raise UnboundVarError(
                        f"variable {e.name!r} is not bound",
                        loc=("program", _node_loc(e)),
                    )
                try:
                    val = env.lookup(e.name, _node_loc(e))
                except _Unbound as u:
                    raise UnboundVarError(
                        f"variable {u.name!r} is not bound",
                        loc=("program", u.where),
                    ) from None
                expr = None
            elif isinstance(e, A.Lam):
                val, expr = Closure(e.param, e.body, env), None
            elif isinstance(e, A.Add):
                k.append(("addR", e.right, env))
                expr = e.left
            elif isinstance(e, A.Call):
                k.append(("callArg", e.arg, env))
                expr = e.fn
            elif isinstance(e, A.Prompt):
                events.append(Event("prompt-enter", e.tag, steps))
                k.append(("prompt", e.tag))
                expr = e.body
            elif isinstance(e, A.Shift):
                cont = _capture_shift(e, k, events, steps)
                # body is evaluated inside a *fresh* same-tag prompt;
                # k is bound to the reified continuation.
                k.append(("prompt", e.tag))
                env = _bind(env, e.param, cont)
                expr = e.body
            else:  # pragma: no cover - validation rejects unknown nodes
                raise BadAstError(f"unknown AST node {type(e).__name__}")
            continue

        # ---------------- return phase ----------------
        assert val is not None
        if not k:
            return Result(val, tuple(events), steps, node_count)

        frame = k.pop()
        kind = frame[0]
        if trace:
            print(
                f"step {steps}: ret {describe_value(val)} through {kind}",
                file=sys.stderr,
            )

        if kind == "addR":
            _, right_expr, saved_env = frame
            if not isinstance(val, IntVal):
                raise _type_error(
                    f"left operand of + is {describe_value(val)}, expected int",
                    right_expr,
                )
            k.append(("addL", val))
            expr, env = right_expr, saved_env
        elif kind == "addL":
            left: IntVal = frame[1]
            if not isinstance(val, IntVal):
                raise _type_error(
                    f"right operand of + is {describe_value(val)}, expected int",
                    None,
                )
            val = IntVal(left.value + val.value)
        elif kind == "callArg":
            _, arg_expr, saved_env = frame
            k.append(("callFn", val))
            expr, env = arg_expr, saved_env
        elif kind == "callFn":
            callee: Value = frame[1]
            if isinstance(callee, Closure):
                env = _bind(callee.env, callee.param, val)
                expr = callee.body
            elif isinstance(callee, ContVal):
                events.append(Event("resume", callee.tag, steps))
                # Fresh same-tag prompt, then captured frames; ``val``
                # flows into the innermost captured frame.  Frames are
                # copied, so the continuation stays reusable.
                k.append(("prompt", callee.tag))
                k.extend(callee.frames)
                # val stays; the return phase continues up the rebuilt stack
            else:
                raise _type_error(
                    f"cannot call non-function value {describe_value(callee)}",
                    None,
                )
        elif kind == "prompt":
            events.append(Event("prompt-exit", frame[1], steps))
            # val remains; keep unwinding
        else:  # pragma: no cover
            raise BadAstError(f"internal: corrupt frame {frame!r}")
