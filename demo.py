"""固定输入演示：一次性续延（shift1）的消费时点。

运行（仓库根目录）：./.venv/bin/python demo.py
约 0.1 秒内完成，无网络/无第三方依赖，无 sleep、无预录结果。

内容：
  1. 正常结果——句柄恰好进入一次，恢复截获的加法上下文。
  2. 别名双调用——句柄经 lambda 参数转手；第二次进入被拒绝
     （ConsumedContinuationError，附截获点/两次调用点位置）。
  3. 参数求值失败还未消费——实参类型错误时检查点尚未到达，
     事件轨迹中没有 once_cont_call。
  4. 进入之后出错仍然已消费——轨迹证明消费先于错误；同程序给足
     预算时第一次调用本可成功（对照打印）。
  5. 一次性续延体内再捕获普通续延——普通续延依旧可多次调用。
"""

from __future__ import annotations

import time

from delimcc import (
    Add,
    Call,
    ConsumedContinuationError,
    Int,
    Lam,
    Prompt,
    RuntimeTypeError,
    Shift,
    ShiftOnce,
    Var,
    interpret,
)

# 位置标记只用 (文件, 行, 列) 基本元组，不嵌入任何宿主函数/代码文本。
LOC_CAPTURE = ("demo.e", 40, 20)
LOC_FIRST = ("demo.e", 41, 9)
LOC_SECOND = ("demo.e", 42, 9)


def section(title: str) -> None:
    print("=" * 68)
    print(title)
    print("-" * 68)


def main() -> None:
    t0 = time.perf_counter()

    # --- 1. 正常：一次性句柄只进入一次 ----------------------------------
    section("[正常] prompt(1, 10 + shift1(k, k(5)))")
    ok_prog = Prompt(
        1,
        Add(Int(10), ShiftOnce(1, "k", Call(Var("k"), Int(5)))),
    )
    r = interpret(ok_prog, trace=True)
    print(f"节点数={r.nodes}  求值步数={r.steps}  结果={r.value.display()}")
    print("检查点事件:", [ev.args for ev in r.events if ev.name == "once_cont_call"])

    # --- 2. 别名双调用 ---------------------------------------------------
    section("[拒绝] 句柄经 (lambda a. a(5)+a(6)) 参数转手，第二次进入")
    # k 作为实参进入 lambda，体内用别名 a 调用两次；三个调用/截获点带 loc。
    alias_prog = Prompt(
        1,
        Add(
            Int(10),
            ShiftOnce(
                1,
                "k",
                Call(
                    Lam(
                        "a",
                        Add(
                            Call(Var("a"), Int(5)).with_loc(LOC_FIRST),
                            Call(Var("a"), Int(6)).with_loc(LOC_SECOND),
                        ),
                    ),
                    Var("k"),
                ),
            ).with_loc(LOC_CAPTURE),
        ),
    )
    try:
        interpret(alias_prog, trace=True)
    except ConsumedContinuationError as ex:
        trail = [(ev.name, ev.args[2]) for ev in ex.events if ev.name == "once_cont_call"]
        rewraps = sum(1 for ev in ex.events if ev.name == "prompt_rewrap_enter")
        print(f"实际捕获: {type(ex).__name__}: {ex}")
        print(f"检查点轨迹: {trail}（fresh=本次消费，consumed=拒绝）")
        print(f"截获上下文的重包只发生 {rewraps} 次：第二次没有重新执行任何截获帧")

    # --- 3. 参数失败还未消费 ---------------------------------------------
    section("[时点] 实参 1 + (lambda z.z) 类型错误：检查点尚未到达")
    arg_fail = Prompt(
        1,
        Add(
            Int(10),
            ShiftOnce(1, "k", Call(Var("k"), Add(Int(1), Lam("z", Var("z"))))),
        ),
    )
    try:
        interpret(arg_fail, trace=True)
    except RuntimeTypeError as ex:
        seq = [ev.name for ev in ex.events]
        print(f"实际捕获: {type(ex).__name__}")
        print(f"事件序列结尾: {seq[-3:]}（没有 once_cont_call => 句柄未消费）")

    # --- 4. 进入后失败仍已消费（轨迹对照） -------------------------------
    section("[时点] k(lambda) 已进入，恢复 10+· 时出错；消费不回滚")
    post_fail = Prompt(
        1,
        Add(Int(10), ShiftOnce(1, "k", Call(Var("k"), Lam("z", Var("z"))))),
    )
    try:
        interpret(post_fail, trace=True)
    except RuntimeTypeError as ex:
        names = [ev.name for ev in ex.events]
        i, j = names.index("once_cont_call"), names.index("prompt_rewrap_enter")
        print(f"实际捕获: {type(ex).__name__}")
        print(f"事件顺序：once_cont_call(第{i+1}) 早于重包进入(第{j+1})，出错在更后")
        print("=> 即使恢复失败，句柄也已消费；语言无异常处理器，无法在同一运行")
        print("   内再次触达该句柄，时点由事件轨迹严格固定。")
    # 对照：同一句柄正常路径给足预算即成功。
    good = Prompt(1, Add(Int(10), ShiftOnce(1, "k", Call(Var("k"), Int(5)))))
    print(f"对照（实参换成 5）= {interpret(good).value.display()}")

    # --- 5. 一次性续延内捕获普通续延 -------------------------------------
    section("[共存] shift1 体内 shift：普通续延 j 仍可两次调用，k 只进入一次")
    mixed = Prompt(
        1,
        Add(
            Int(10),
            ShiftOnce(
                1,
                "k",
                Shift(
                    1,
                    "j",
                    Add(
                        Call(Var("j"), Call(Var("k"), Int(5))),
                        Call(Var("j"), Int(2)),
                    ),
                ),
            ),
        ),
    )
    rm = interpret(mixed, trace=True)
    print(f"结果={rm.value.display()}（j 恒等，k 唯一一次恢复 10+5=15；15+2=17）")
    print(
        "事件计数:",
        f"cont_call={sum(1 for e in rm.events if e.name == 'cont_call')}",
        f"once_cont_call={sum(1 for e in rm.events if e.name == 'once_cont_call')}",
    )

    print("=" * 68)
    print(f"演示完成，实际耗时 {time.perf_counter() - t0:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
