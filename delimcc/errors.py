"""Error semantics for the delimited-continuation interpreter.

All errors derive from :class:`DlmccError` so callers can catch the whole
family.  Every error carries a machine-readable ``code`` and, where the
problem is a node inside the AST, a ``loc`` tuple describing the node path
(e.g. ``("node#3", "body")``), so failures are locatable rather than silent
corrections.
"""

from __future__ import annotations


class DlmccError(Exception):
    """Base class for every error raised by this package."""

    code = "dlmcc-error"

    def __init__(self, message: str, *, loc: tuple[str, ...] = ()) -> None:
        self.loc = loc
        suffix = f" [at {' > '.join(loc)}]" if loc else ""
        super().__init__(message + suffix)


class NodeLimitError(DlmccError):
    """AST has more nodes than the interface allows (default 1000)."""

    code = "node-limit"


class StepLimitError(DlmccError):
    """Evaluation exceeded its step budget (default 20000).

    This is *budget exhaustion*, distinct from :class:`NoPromptError` which
    proves that a ``shift`` can never find its prompt.
    """

    code = "step-limit"


class NoPromptError(DlmccError):
    """A ``shift`` with no matching ``prompt`` tag was reached.

    Proven unanswerable: there is no enclosing prompt with the same tag in
    the current continuation, so no amount of extra steps could succeed.
    """

    code = "no-prompt"


class UnboundVarError(DlmccError):
    """A variable name was looked up but not bound in any environment."""

    code = "unbound-var"


class BadAstError(DlmccError):
    """An AST node failed structural validation (shape/types/fields)."""

    code = "bad-ast"
