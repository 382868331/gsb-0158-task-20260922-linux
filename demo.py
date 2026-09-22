#!/usr/bin/env python3
"""Fixed-input demonstration for the delimcc package.

Run:  python demo.py

It performs two real evaluations (no sleep, no pre-recorded output):

1. Normal result -- a *non-local return*: a function called from deep in
   evaluation aborts its pending context through a prompt, like an early
   ``return`` skipping the rest of a computation.

2. Actual rejection boundary -- a ``shift`` whose tag matches no enclosing
   prompt is a provably impossible reduction; the interpreter raises
   ``NoPromptError`` rather than guessing a value.  (Budget exhaustion is a
   different error class and is not conflated with it.)
"""

import time

from delimcc import (
    Add,
    Call,
    Int,
    Lam,
    NoPromptError,
    Prompt,
    Shift,
    Var,
    describe_value,
    evaluate,
)


def section(title: str) -> None:
    print("=" * 68)
    print(title)
    print("=" * 68)


def main() -> int:
    # ------------------------------------------------------------------ #
    # 1. Normal result: non-local return through prompt tag 0.           #
    #                                                                    #
    # prompt0 {                                                          #
    #   999 + ((lam f. f(1)) (lam x. shift0 k. x))                       #
    # }                                                                  #
    # Ordinary call-by-value flow would finish "999 + []" and get 1000. #
    # But f(1) invokes (lam x. shift0 k. x): the shift captures the      #
    # context up to prompt0 ("999 + []" and the pending call), discards  #
    # it, and the prompt evaluates to x = 1 -- an early return that      #
    # skips all pending work.                                            #
    # ------------------------------------------------------------------ #
    section("1. Normal result: non-local (early) return")
    program = Prompt(
        0,
        Add(
            Int(999),
            Call(
                Lam("f", Call(Var("f"), Int(1))),
                Lam("x", Shift(0, "k", Var("x"))),
            ),
        ),
    )
    print("program:")
    print("  prompt0 { 999 + ((lam f. f(1)) (lam x. shift0 k. x)) }")

    t0 = time.perf_counter()
    result = evaluate(program)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    print(f"result value      : {describe_value(result.value)}")
    print(f"result as integer : {result.as_int()}")
    print(f"AST nodes         : {result.node_count}")
    print(f"CEK steps         : {result.steps}")
    print("events            :")
    for ev in result.events:
        print(f"  step {ev.step:>3}: {ev.kind} (tag={ev.tag})")
    print(f"elapsed           : {elapsed_ms:.3f} ms")
    assert result.as_int() == 1, "non-local return must yield 1, not 1000"
    print("-> the pending '999 + []' was skipped; early return delivered 1.")

    print()

    # ------------------------------------------------------------------ #
    # 2. Actual triggered boundary: shift without a matching prompt.     #
    # ------------------------------------------------------------------ #
    section("2. Rejection boundary: shift with no matching prompt")
    bad_program = Add(Int(1), Prompt(7, Shift(3, "k", Int(0))))
    print("program:")
    print("  1 + prompt7 { shift3 k. 0 }   # tag 3 has no enclosing prompt")
    try:
        evaluate(bad_program)
    except NoPromptError as exc:
        print(f"raised            : {type(exc).__name__} (code={exc.code})")
        print(f"message           : {exc}")
        print("-> proven impossible: no same-tag prompt exists in the")
        print("   continuation, so no step budget could ever produce a value.")
    else:  # pragma: no cover - the boundary must trigger
        print("UNEXPECTED: no error was raised")
        return 1

    print()
    print("Both evaluations completed well within the demo time budget.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
