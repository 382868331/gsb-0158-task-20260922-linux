# delimcc：多重分隔提示的定界续延解释器

一个零依赖的 Python 题库，实现带多 tag 分隔提示（delimited control）的
AST 解释器：整数、加法、变量、lambda、调用、`prompt(tag, body)`、
`shift(tag, k, body)`（可多次进入的续延）与
`shift_once(tag, k, body)`（只能消费一次的一次性续延），无可变引用。

## 运行环境

- **Python 3.14.7**（仅使用标准库；3.10+ 亦可运行，目标版本为 3.14.7）
- Linux 原生运行，**安装 Python 后无需任何额外依赖**
- 核心、测试、演示全部离线；不访问网络、不需要账号/密钥/数据库/中间件/
  云 API/Docker

## 快速命令

```bash
# 执行全部必要测试（在仓库根目录）
./.venv/bin/python -m unittest discover -s tests -v

# 固定输入演示：非局部返回 + 一次性续延 + 实际触发的拒绝/异常边界
./.venv/bin/python demo.py
```

演示真实即时计算（约 0.03 秒，远低于 8 秒预算），无 `sleep`、无预录
结果、不做压力测试。

## 语言与语义

| 构造 | 含义 |
| --- | --- |
| `Int(n)` | 任意 Python 整数 |
| `Var(x)` | 变量引用 |
| `Add(l, r)` | 整数加法，先求左再求右 |
| `Lam(x, body)` | 单参数词法闭包 |
| `Call(f, a)` | 按值调用，先求函数再求实参 |
| `Prompt(tag, body)` | 为整数 `tag` 设立定界提示并求 body |
| `Shift(tag, k, body)` | 捕获到**最近**同 tag prompt 的求值上下文（多次续延） |
| `ShiftOnce(tag, k, body)` | 同上，但 `k` 是**只能进入一次**的一次性句柄 |

求值规则（按值、左到右）：

1. `Prompt(t, e)`：压入 tag=t 的定界帧后求 `e`；body 正常完成时其值
   就是 prompt 的值。
2. `Shift(t, k, e)` / `ShiftOnce(t, k, e)`：自当前求值点向栈底找最近
   的同 tag 定界帧。
   - **找到**：该帧与当前点之间的上下文被捕获为续延值并绑定到
     `k`；定界帧保留，`e` 在该 prompt **之内**求值。若 `e` 不调用
     `k`，被捕获的上下文被放弃——即非局部返回。
   - **找不到**：抛 `UncaughtShiftError`（确定性语义错误）。
3. 调用**多次续延** `k(v)` 是组合式（composable）恢复：以 `v` 为洞值
   驱动捕获的上下文，其外重新包裹一个同 tag 的 prompt，调用点本身的
   上下文完整保留。续延值不可变，**可多次调用**，每次行为一致。
4. 调用**一次性句柄** `k(v)` 分解为可观察的小步：
   - 先按通常规则求值函数位置与**实参**（实参求值失败——包括被其他
     tag 的 shift 中断——时句柄**尚未消费**）；
   - 实参求值完毕后**原子地“检查并消费”**句柄；
   - 随后才压入重包 prompt 并进入截获的上下文。进入之后无论出错
     （类型错误等）还是预算耗尽，句柄**仍然已消费**，不存在回滚/
     复活路径。
   - 第二次进入（无论句柄经多少变量别名转手）在消费点抛
     `ContinuationConsumedError`，**不重新执行截获的上下文**；错误
     携带首次消费的来源位置与本次调用位置（来自 `Call` 节点的
     `loc`）。
5. tag 是显式整数；不同 tag 的提示相互独立、可嵌套，`Shift` 与
   `ShiftOnce` 都按“最近同 tag prompt”规则捕获。

## 公开接口

```python
from delimcc import (
    # AST
    Int, Var, Add, Lam, Call, Prompt, Shift, ShiftOnce,
    # 求值
    interpret, Event, Result,
    # 运行时值
    IntVal, CloVal, ContVal, OnceContVal,
    # 错误
    DelimccError, InvalidProgramError, EvaluationError,
    UncaughtShiftError, UnboundVariableError, RuntimeTypeError,
    ContinuationConsumedError, StepLimitError,
)

r = interpret(Prompt(1, Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5))))))
r.value.display()      # -> "15"（IntVal/CloVal/ContVal/OnceContVal 均有 display()）
r.nodes, r.steps       # 节点数、实际机器步数
r2 = interpret(prog, trace=True)
r2.events              # 求值事件元组（见下）
```

`interpret(program, *, trace=False, max_nodes=1000, max_steps=20000)`
返回 `Result(value, events, nodes, steps)`。

事件（`trace=True`）：`prompt_enter/prompt_exit(tag[, 值])`（源码
prompt）、`prompt_rewrap_enter/prompt_rewrap_exit`（`k(v)` 恢复时的重包
prompt）、`shift_capture(tag, 捕获帧数)`、`once_capture(tag, 捕获帧数)`、
`cont_call(tag, 帧数)`、`once_consume(tag, 帧数, 调用位置loc)`（一次性
句柄的原子消费点，发生在实参求值完毕之后、重包 prompt 之前）、
`add(l, r, s)`、`result(值)`。

`trace=True` 时若求值以 `EvaluationError` 告终，异常对象上附带
`.events`（截至失败点的部分事件轨迹），可用于核对一次性句柄的消费
时点；异常类型与消息不受影响。

## 输入上限（接口边界）

| 边界 | 默认值 | 超出时的行为 |
| --- | --- | --- |
| AST 节点总数 | **1000** | 求值开始前抛 `InvalidProgramError`（可用 `max_nodes` 放宽） |
| 求值机器步数 | **20000** | 抛 `StepLimitError`（预算耗尽） |

- 节点数在求值前以迭代遍历统计（深树不触发 Python 递归限制）。
- **预算耗尽 ≠ 无解**：`StepLimitError`（资源预算）与
  `UncaughtShiftError`（语义上不存在匹配 prompt，确定性无解）是两个
  独立异常类型，调用方可分别捕获。
- 短演示不穷举上限规模；1000/20000 是接口契约边界。

## 错误语义（可定位，不静默纠正）

- 构造期（`InvalidProgramError`）：tag 必须是非 bool 的 `int`；
  `Int` 拒绝 `bool`、一切 `float`（含 **NaN/Infinity**）；变量/参数名
  必须是非空 `str`；子节点必须是 `Expr`。错误消息附字段名，带 `loc`
  的节点还会报告 `文件:行:列`。
- 求值期：`UnboundVariableError`（未绑定变量）、
  `RuntimeTypeError`（对非整数做加法/调用非函数值）、
  `UncaughtShiftError`（无匹配 prompt）、
  `ContinuationConsumedError`（一次性句柄第二次进入；`.consumed_loc`
  为首次消费来源位置、`.attempt_loc` 为本次调用位置，均可为 None）、
  `StepLimitError`（超步）。
- 求值无副作用、无部分提交概念；AST 与运行时值均不可变，失败不留
  未说明的状态变更。唯一例外是一次性句柄的消费标记（单调
  False→True，只进不退）：它是“只能进入一次”语义的载体，句柄不
  提供重置接口，解释器也没有回滚路径——进入之后出错或预算耗尽都
  不会让句柄复活。

## 设计取舍

- **核心是 CEK 风格的显式栈迭代机器**（`delimcc/interpreter.py`）：
  环境是单绑定不可变链表，控制栈是不可变帧元组；全程不使用 Python
  递归，嵌套深度只受步数预算约束。`shift`/`shift_once` 通过栈上线性
  查找最近同 tag 帧实现；`k(v)` 通过栈拼接（调用点栈 + 重包 prompt
  帧 + 捕获帧）实现组合式恢复。
- **一次性句柄**（`OnceContVal`）与多次续延（`ContVal`）是两种值：
  捕获的上下文同样不可变，区别只在调用点——`callArg` 帧返回时先
  `consume(loc)` 原子检查并置位，再压重包帧。消费来源位置取自
  `Call` 节点的 `loc`（随 `callFn`/`callArg` 帧传递），只记录位置
  三元组，不序列化宿主函数、不执行用户 Python 文本。
- **独立参考实现**（`tests/reference.py`）刻意不导入核心：自带元组
  s-表达式语法，用“显式求值上下文 + 避免捕获替换（alpha 改名）”的
  小步规约机实现同一语义；`shift` 把 k 具体化为
  `λx. prompt(t, E_t[x])`，续延即可复制的上下文快照。两套算法仅共享
  题目语义。参考机覆盖原多次续延语义；一次性句柄的契约由
  `tests/test_oneshot.py` 的手写小步事件序列直接核对。
- 交叉测试（`tests/test_crosscheck.py`）中期望值只由参考机产生；
  另含 120 个固定种子确定性程序的双机差分（`tests/program_gen.py`，
  LCG 固定种子，仓库内生成、可复现）。开发期另跑过 3000 种子差分
  （非提交测试的一部分）。
- 闭包持有不可变 AST body 的共享引用；因为 AST 不可变且语言无赋值，
  共享不引入别名问题。
- 仅实现本题的有限数学/控制子集：整数运算不限定为固定位宽（用 Python
  大整数），不实现浮点数、字符串、可变引用、多参数/递归绑定，也不
  宣称与任何行业标准效果系统兼容。

## 浮点与误差口径

本语言**只有整数，没有浮点类型**，故不存在浮点舍入误差；任何
`float`（包括 `NaN`、`±Infinity`）作为字面量都在构造期被拒绝，
不会发生隐式 int/float 转换，也不以宽容差掩盖边界错误。

## 仓库结构

```
delimcc/
  __init__.py     公开导出
  ast.py          不可变 AST 与构造期校验（含 ShiftOnce）
  errors.py       错误类型体系（含 ContinuationConsumedError）
  values.py       运行时值（IntVal/CloVal/ContVal/OnceContVal）
  interpreter.py  CEK 显式栈解释器（核心算法）
tests/
  reference.py    独立参考：显式上下文小步替换机（不导入核心）
  program_gen.py  确定性程序生成器（固定种子 LCG）
  test_core.py    核心语义/校验/边界测试
  test_oneshot.py 一次性续延：消费时点、别名双调用、失败语义测试
  test_crosscheck.py  参考机对照与差分、独立性检查、幂等补充
demo.py           固定输入演示
```

## 已知限制

- 单参数 lambda；无内置递归绑定（但续延/闭包为一等值）。
- tag 比较为 Python 整数相等；不支持符号 tag。
- 事件与栈帧结构是观察/调试接口，帧内部形状不保证跨版本稳定。
- 一次性句柄的消费状态只在单次 `interpret` 调用内有效；句柄值不能
  跨求值持久化，也不提供序列化。
- 参考机使用替换语义，深嵌套程序中替换成本可能较高（仅用于验收，
  非生产路径；自身带 200000 步保护）。

