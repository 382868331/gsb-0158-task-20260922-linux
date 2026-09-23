"""固定输入演示：定界续延 + dynamic-wind 作用域钩子。

运行：python demo.py（仓库根目录，1 秒内完成，无网络/无第三方依赖）。

内容：
  1. 正常结果——非局部返回：多层上下文包围 shift，body 直接给出 999，
     中间上下文被放弃（旧能力，保持可用）。
  2. 本次新能力 wind(before, body, after)：
     a. 正常进入/返回的钩子顺序（值取 body，钩子值丢弃）；
     b. shift 捕获跨越作用域、k(v) 恢复时重入：capture-after 与
        resume-before/normal-after 的完整事件顺序（手算值 15）；
     c. 实际错误：body 类型错误时作用域自内向外执行 error-after。
  3. 输入边界：钩子中静态出现 shift 在构造期即被拒绝；以及无匹配
     prompt 的 UncaughtShiftError。
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
    RuntimeTypeError,
    UncaughtShiftError,
    Var,
    Wind,
    interpret,
)


def section(title: str) -> None:
    print("=" * 68)
    print(title)
    print("-" * 68)


def wind_events(result):
    return [
        (ev.name,) + tuple(ev.args)
        for ev in result.events
        if ev.name in ("wind_before", "wind_after")
    ]


def main() -> None:
    t0 = time.perf_counter()

    # --- 1. 旧能力保持可用：非局部返回 --------------------------------
    section("[正常] 非局部返回：上下文被放弃，shift body 直接交回 999")
    program = Prompt(
        1,
        Add(
            Call(
                Lam("a", Add(Var("a"), Int(7))),
                Call(Lam("b", Add(Var("b"), Var("b"))), Shift(1, "k", Int(999))),
            ),
            Int(1),
        ),
    )
    r = interpret(program, trace=True)
    print(f"=> 结果 {r.value.display()}（不是普通求值的 2006），步数={r.steps}")

    # --- 2a. wind 正常进入/返回 ---------------------------------------
    section("[新能力·正常] wind(before=11, body=22, after=33)")
    r = interpret(Wind(Int(11), Int(22), Int(33)), trace=True)
    print(f"=> 结果 {r.value.display()}（取 body；钩子值 11/33 丢弃）")
    print("   钩子事件:", wind_events(r))

    # --- 2b. 捕获跨越作用域 + 恢复重入（核心场景） --------------------
    section("[新能力·跨作用域恢复] prompt( wind(100, 10 + shift(k, k(5)), 200) )")
    crossing = Prompt(
        1,
        Wind(
            Int(100),
            Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5)))),
            Int(200),
        ),
    )
    r = interpret(crossing, trace=True)
    print(f"=> 结果 {r.value.display()}（手算：捕获区为 10+·，k(5)=15）")
    print("   作用域事件顺序：")
    for ev in wind_events(r):
        print("     ", ev)

    # --- 2c. 实际错误：异常退出触发 error-after -----------------------
    section("[新能力·错误] body 内类型错误，两层作用域自内向外 error 退出")
    bad = Wind(
        Int(1),
        Wind(Int(2), Add(Int(1), Lam("z", Var("z"))), Int(4)),
        Int(3),
    )
    try:
        interpret(bad, trace=True)
    except RuntimeTypeError as ex:
        print(f"=> 实际捕获 {type(ex).__name__}: {ex}")
        print("   失败前钩子事件（trace 随错误对象返回）：")
        for ev in ex.events:
            if ev.name in ("wind_before", "wind_after"):
                print("     ", (ev.name,) + tuple(ev.args))

    # --- 3. 输入/语义边界 ---------------------------------------------
    section("[边界] 钩子里静态写 shift 被构造期拒绝；shift 无 prompt 报错")
    try:
        Wind(Shift(1, "k", Int(0)), Int(1), Int(2))
    except InvalidProgramError as ex:
        print(f"Wind(shift,...) -> {type(ex).__name__}: {ex}")
    try:
        interpret(Add(Int(1), Shift(404, "k", Int(0))))
    except UncaughtShiftError as ex:
        print(f"无匹配 prompt -> {type(ex).__name__}: {ex}")

    print("=" * 68)
    print(f"演示完成，实际耗时 {time.perf_counter() - t0:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
