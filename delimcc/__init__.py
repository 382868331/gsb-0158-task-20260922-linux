"""delimcc -- multi-prompt delimited-continuation AST interpreter.

Public API
----------
AST nodes (frozen dataclasses):
    Int(value), Add(left, right), Var(name), Lam(param, body),
    Call(fn, arg), Prompt(tag, body), Shift(tag, param, body)

Functions:
    evaluate(program, *, max_steps=20000, max_nodes=1000, trace=False) -> Result
    validate(node)
    describe_value(value)

Result
    .value      -- IntVal | Closure | ContVal
    .events     -- tuple[Event]  (kind, tag, step)
    .steps      -- transitions taken
    .node_count -- validated AST size
    .as_int()   -- integer payload or BadAstError

Errors (all subclass DlmccError, all carry .code and .loc):
    BadAstError, NodeLimitError, StepLimitError, NoPromptError,
    UnboundVarError
"""

from __future__ import annotations

from . import ast, reference
from .ast import (
    DEFAULT_MAX_NODES,
    Add,
    Call,
    Int,
    Lam,
    Node,
    Prompt,
    Shift,
    Var,
    check_program,
    count_nodes,
    validate,
)
from .core import (
    DEFAULT_MAX_STEPS,
    Closure,
    ContVal,
    Event,
    IntVal,
    Result,
    describe_value,
    run,
)
from .errors import (
    BadAstError,
    DlmccError,
    NoPromptError,
    NodeLimitError,
    StepLimitError,
    UnboundVarError,
)


def evaluate(program, *, max_steps=DEFAULT_MAX_STEPS, max_nodes=DEFAULT_MAX_NODES,
             trace=False):
    """Validate and evaluate an AST; see :func:`delimcc.core.run`."""
    return run(program, max_steps=max_steps, max_nodes=max_nodes, trace=trace)


__all__ = [
    "ast", "reference",
    # nodes
    "Node", "Int", "Add", "Var", "Lam", "Call", "Prompt", "Shift",
    # values / result
    "IntVal", "Closure", "ContVal", "Event", "Result", "describe_value",
    # api
    "evaluate", "run", "validate", "check_program", "count_nodes",
    # limits
    "DEFAULT_MAX_NODES", "DEFAULT_MAX_STEPS",
    # errors
    "DlmccError", "BadAstError", "NodeLimitError", "StepLimitError",
    "NoPromptError", "UnboundVarError",
]
