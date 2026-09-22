"""Public AST data structures, validation and node accounting.

The language is a deliberately small mathematical subset; it is *not* an
industry-standard language and makes no claim of one.

Node grammar::

    e ::= Int(value)
        | Add(left, right)
        | Var(name)
        | Lam(param, body)
        | Call(fn, arg)
        | Prompt(tag, body)
        | Shift(tag, param, body)

Contract details
----------------
* Integer slots (``value`` and every ``tag``) must be Python ``int``.
  ``bool`` is rejected even though it is an ``int`` subclass, and floats
  (including ``nan``/``inf``) are rejected with an explicit message rather
  than silently rounded.
* Names must be non-empty ``str``.
* Child fields must be :class:`Node` instances.
* A program may contain at most ``max_nodes`` nodes (interface limit,
  default 1000).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import ClassVar

from .errors import BadAstError, NodeLimitError

#: Interface boundary: maximum number of AST nodes in one program.
DEFAULT_MAX_NODES = 1000


class Node:
    """Base class of every AST node."""

    #: Child node attribute names, in left-to-right evaluation order.
    _children: ClassVar[tuple[str, ...]] = ()

    def children(self) -> tuple["Node", ...]:
        return tuple(getattr(self, f) for f in self._children)


def _reject_non_int(x: object, field: str, loc: tuple[str, ...]) -> int:
    if isinstance(x, bool) or not isinstance(x, int):
        if isinstance(x, float) and math.isnan(x):
            raise BadAstError(f"field {field!r} rejects NaN; integer required", loc=loc)
        if isinstance(x, float) and math.isinf(x):
            kind = "Infinity" if x > 0 else "-Infinity"
            raise BadAstError(
                f"field {field!r} rejects {kind}; integer required", loc=loc
            )
        raise BadAstError(
            f"field {field!r} must be int (bool is rejected), "
            f"got {type(x).__name__}",
            loc=loc,
        )
    return x


def _reject_non_name(x: object, field: str, loc: tuple[str, ...]) -> str:
    if not isinstance(x, str) or x == "":
        raise BadAstError(
            f"field {field!r} must be a non-empty str, got {type(x).__name__}",
            loc=loc,
        )
    return x


def _validate(node: object, loc: tuple[str, ...]) -> None:
    if not isinstance(node, Node):
        raise BadAstError(
            f"expected AST Node, got {type(node).__name__}", loc=loc
        )
    for f in fields(node):
        val = getattr(node, f.name)
        child_loc = loc + (f"{type(node).__name__}.{f.name}",)
        if f.name in node._children:
            _validate(val, child_loc)
        elif f.name == "value" or f.name == "tag":
            _reject_non_int(val, f.name, child_loc)
        elif f.name in ("name", "param"):
            _reject_non_name(val, f.name, child_loc)


@dataclass(frozen=True)
class Int(Node):
    """Integer literal."""

    value: int
    _children: ClassVar[tuple[str, ...]] = ()


@dataclass(frozen=True)
class Add(Node):
    """Integer addition; both sides evaluated left-to-right."""

    left: Node
    right: Node
    _children: ClassVar[tuple[str, ...]] = ("left", "right")


@dataclass(frozen=True)
class Var(Node):
    """Variable reference."""

    name: str
    _children: ClassVar[tuple[str, ...]] = ()


@dataclass(frozen=True)
class Lam(Node):
    """Single-argument lambda (lexical closure at runtime)."""

    param: str
    body: Node
    _children: ClassVar[tuple[str, ...]] = ("body",)


@dataclass(frozen=True)
class Call(Node):
    """Function call; callee then argument, call-by-value."""

    fn: Node
    arg: Node
    _children: ClassVar[tuple[str, ...]] = ("fn", "arg")


@dataclass(frozen=True)
class Prompt(Node):
    """Delimited-prompt boundary tagged with an explicit integer."""

    tag: int
    body: Node
    _children: ClassVar[tuple[str, ...]] = ("body",)


@dataclass(frozen=True)
class Shift(Node):
    """Capture up to the nearest ``Prompt`` with the same integer ``tag``.

    ``param`` is the variable bound to the reified continuation inside
    ``body``; ``body`` is evaluated within the matched prompt.
    """

    tag: int
    param: str
    body: Node
    _children: ClassVar[tuple[str, ...]] = ("body",)


def validate(node: Node) -> None:
    """Validate ``node`` recursively, raising :class:`BadAstError` on problems.

    The error's ``loc`` is a path of attribute names from the root, making
    failures locatable.  Nothing is silently corrected.
    """
    _validate(node, ("program",))


def count_nodes(node: Node) -> int:
    """Count AST nodes (every node counts, including the root)."""
    if not isinstance(node, Node):
        raise BadAstError(
            f"cannot count non-Node object of type {type(node).__name__}",
            loc=("program",),
        )
    total = 1
    for child in node.children():
        total += count_nodes(child)
    return total


def check_program(node: Node, max_nodes: int = DEFAULT_MAX_NODES) -> int:
    """Validate and enforce the node budget; returns the node count."""
    validate(node)
    n = count_nodes(node)
    if n > max_nodes:
        raise NodeLimitError(
            f"program has {n} nodes, limit is {max_nodes}",
            loc=("program",),
        )
    return n
