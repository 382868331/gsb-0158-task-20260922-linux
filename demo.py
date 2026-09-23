"""固定输入演示：定界续延的非局部返回，以及 wind 作用域钩子。

运行：python demo.py（仓库根目录，毫秒级完成，远低于 90 秒预算；
无网络、无第三方依赖、无 sleep、无预录结果）。

内容
----
1. 正常结果——非局部返回：多层上下文包围 shift，body 不调用 k 直接
   交回 999，中间上下文被放弃。
2. 本次新增——``wind(before, body, after)``：
   a. 正常嵌套：进入外->内、离开内->外，值取 body；
   b. 跨作用域捕获/恢复：shift 离开作用域跑 after，k(v) 恢复重入跑
      before，值穿越重放作用域时再跑 after（事件即时由解释器产生）；
   c. 异常退出：body 出错时由内向外补跑 after 后重抛原错；
   d. 违约：钩子里 shift 被 HookViolationError 拒绝。
3. 语义边界——shift 找不到同 tag prompt：UncaughtShiftError。
4. 输入边界——NaN / bool 不是合法整数字面量：构造期拒绝。
"""

from __future__ import annotations

import time

from delimcc import (
    Add,
    Call,
    HookViolationError,
    Int,
    InvalidProgramError,
    Lam,
    Prompt,
    RuntimeTypeError,
    Shift,
    UncaughtShiftError,
    Var,
    Wind,
    interpret,
)


def section(title: str) -> None:
    print("=" * 68)
    print(title)
    print("-" * 68)


def wind_trace(program):
    """求 wind 程序，返回 (值展示, 仅 wind 进出事件名序列)。"""
    r = interpret(program, trace=True)
    seq = [e.name.removeprefix("wind_") for e in r.events
           if e.name in ("wind_before", "wind_after")]
    return r.value.display(), seq


def main() -> None:
    t0 = time.perf_counter()

    # --- 1. 正常结果：非局部返回 ---------------------------------------
    section("[正常] 非局部返回：prompt(1, (a+7)((b+b){shift => 999}) + 1)")
    program = Prompt(
        1,
        Add(
            Call(
                Lam("a", Add(Var("a"), Int(7))),
                Call(
                    Lam("b", Add(Var("b"), Var("b"))),
                    Shift(1, "k", Int(999)),
                ),
            ),
            Int(1),
        ),
    )
    result = interpret(program, trace=True)
    captured = [a for ev in result.events if ev.name == "shift_capture"
                for a in ev.args]
    print(f"节点数={result.nodes}  求值步数={result.steps}")
    print(f"shift 捕获 tag={captured[0]}，捕获帧数={captured[1]}（随后被放弃）")
    print(f"=> 最终结果: {result.value.display()}（非局部返回，不是 2006）")
    ordinary = Add(
        Call(Lam("a", Add(Var("a"), Int(7))),
             Call(Lam("b", Add(Var("b"), Var("b"))), Int(999))),
        Int(1),
    )
    print(f"   对照（无 shift 的普通求值）= {interpret(ordinary).value.display()}")

    # --- 2a. wind 正常嵌套 LIFO ----------------------------------------
    section("[wind 正常] 嵌套作用域：wind(b10, wind(b20, 30, a21), a11)")
    nested = Wind(Int(10), Wind(Int(20), Int(30), Int(21)), Int(11))
    value, seq = wind_trace(nested)
    print("钩子进出顺序（即时产生）:", " -> ".join(seq))
    print(f"=> 结果 {value}（before/after 的值被丢弃，取 body 值 30）")

    # --- 2b. wind 跨作用域捕获 / 恢复 ----------------------------------
    section("[wind × shift] 捕获离开 -> k(v) 恢复重入 -> 值再离开")
    crossing = Prompt(
        1,
        Wind(
            Int(2),
            Add(Int(100), Shift(1, "k", Call(Var("k"), Int(5)))),
            Int(3),
        ),
    )
    value, seq = wind_trace(crossing)
    print("钩子进出顺序（即时产生）:", " -> ".join(seq))
    print("  第 1 对：进入 body；shift 捕获把作用域留在身后 -> after")
    print("  第 2 对：k(5) 恢复重放作用域 -> before；100+5 穿回 -> after")
    print(f"=> 结果 {value}（= 100 + 5；重放不改变洞值算术）")

    section("[wind × shift] 两层作用域都被捕获，跨作用域恢复顺序")
    two = Prompt(
        1,
        Wind(
            Int(11),
            Wind(Int(22), Add(Int(100), Shift(1, "k", Call(Var("k"), Int(5)))),
                 Int(33)),
            Int(44),
        ),
    )
    value, seq = wind_trace(two)
    print("钩子进出顺序（即时产生）:", " -> ".join(seq))
    print("  离开内->外；重入外->内；值离开重放再内->外")
    print(f"=> 结果 {value}")

    # --- 2c. wind 异常退出补跑 after -----------------------------------
    section("[wind 异常退出] body 类型错误：由内向外补跑 after 后重抛原错")
    err_prog = Wind(Int(1), Add(Int(1), Lam("x", Var("x"))), Int(99))
    try:
        interpret(err_prog, trace=True)
    except RuntimeTypeError as ex:
        seq = [e.name.removeprefix("wind_") for e in ex.events
               if e.name in ("wind_before", "wind_after")]
        print(f"实际捕获异常: {type(ex).__name__}: {ex}")
        print("抛出前已执行的钩子:", " -> ".join(seq))
        print("=> 异常路径也保证作用域被正常离开。")

    # --- 2d. 钩子契约：钩子里不许捕获续延 ------------------------------
    section("[wind 违约] before 钩子里出现 shift：明确拒绝，不留半状态")
    bad_hook = Prompt(1, Wind(Shift(1, "k", Int(0)), Int(2), Int(3)))
    try:
        interpret(bad_hook)
    except HookViolationError as ex:
        print(f"实际捕获异常: {type(ex).__name__}: {ex}")
        print("=> 钩子只负责记录事件，不在钩子里捕获/调用续延。")

    # --- 3. 语义边界：无匹配 prompt -------------------------------------
    section("[边界] shift(404, k, 0) 外没有 tag=404 的 prompt")
    bad_program = Add(Int(1), Shift(404, "k", Int(0)))
    try:
        interpret(bad_program)
    except UncaughtShiftError as ex:
        print(f"实际捕获异常: {type(ex).__name__}: {ex}")
        print("=> 确定性语义错误（无解），区别于步数预算耗尽。")

    # --- 4. 输入边界：NaN / bool 不得作为整数 ---------------------------
    section("[边界] Int(NaN) 与 Int(True) 在构造期被拒绝")
    for label, bad in (("float('nan')", float("nan")), ("True", True)):
        try:
            Int(bad)
        except InvalidProgramError as ex:
            print(f"Int({label}) -> {type(ex).__name__}: {ex}")

    print("=" * 68)
    print(f"演示完成，实际耗时 {time.perf_counter() - t0:.3f} 秒"
          "（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
