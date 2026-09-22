"""固定输入演示：定界续延的非局部返回，以及两个真实触发的拒绝边界。

运行：python demo.py（仓库根目录，约 1 秒内完成，无网络/无第三方依赖）。

内容：
  1. 正常结果——非局部返回：多层函数/加法上下文包围 shift，shift 的
     body 不调用续延 k，直接给出 999，中间上下文被整体放弃。
  2. 语义边界——shift 找不到同 tag 的 prompt，实际抛出
     UncaughtShiftError（无解，与预算耗尽区分）。
  3. 输入边界——NaN 不是合法整数字面量，构造时即被
     InvalidProgramError 拒绝（不静默纠正）。
"""

from __future__ import annotations

import time

from delimcc import (
    Add,
    Call,
    Int,
    InvalidProgramError,
    Lam,
    Prompt,
    Shift,
    UncaughtShiftError,
    Var,
    interpret,
)


def section(title: str) -> None:
    print("=" * 64)
    print(title)
    print("-" * 64)


def main() -> None:
    t0 = time.perf_counter()

    # --- 1. 正常结果：非局部返回 ---------------------------------------
    section("[正常] 非局部返回：prompt(1, (a+7)((b+b){shift => 999}) + 1)")
    program = Prompt(
        1,
        Add(
            Call(
                Lam("a", Add(Var("a"), Int(7))),      # 若正常返回本应 +7
                Call(
                    Lam("b", Add(Var("b"), Var("b"))),  # 若正常返回本应翻倍
                    Shift(1, "k", Int(999)),            # 直接交回 999，放弃 k
                ),
            ),
            Int(1),
        ),
    )
    result = interpret(program, trace=True)
    captured = [a for ev in result.events if ev.name == "shift_capture" for a in ev.args]
    print(f"节点数={result.nodes}  求值步数={result.steps}")
    print(f"shift 捕获 tag={captured[0]}，捕获帧数={captured[1]}（这些帧随后被放弃）")
    print(f"=> 最终结果: {result.value.display()}（非局部返回，不是 (999*2+7)+1）")

    # 对照：若不用 shift，普通求值应为 (999*2+7)+1 = 2006。
    ordinary = Add(
        Call(Lam("a", Add(Var("a"), Int(7))),
             Call(Lam("b", Add(Var("b"), Var("b"))), Int(999))),
        Int(1),
    )
    print(f"   对照（无 shift 的普通求值）= {interpret(ordinary).value.display()}")

    # --- 2. 语义边界：无匹配 prompt -------------------------------------
    section("[边界] shift(404, k, 0) 外没有 tag=404 的 prompt")
    bad_program = Add(Int(1), Shift(404, "k", Int(0)))
    try:
        interpret(bad_program)
    except UncaughtShiftError as ex:
        print(f"实际捕获异常: {type(ex).__name__}: {ex}")
        print("=> 这是确定性语义错误（无解），区别于步数预算耗尽。")

    # --- 3. 输入边界：NaN / bool 不得作为整数 ---------------------------
    section("[边界] Int(NaN) 与 Int(True) 在构造期被拒绝")
    for label, bad in (("float('nan')", float("nan")), ("True", True)):
        try:
            Int(bad)
        except InvalidProgramError as ex:
            print(f"Int({label}) -> {type(ex).__name__}: {ex}")

    print("=" * 64)
    print(f"演示完成，实际耗时 {time.perf_counter() - t0:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
