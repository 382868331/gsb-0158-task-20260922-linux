# delimcc：多重分隔提示的定界续延解释器（含一次性续延）

一个零依赖的 Python 题库，实现带多 tag 分隔提示（delimited control）的
AST 解释器：整数、加法、变量、lambda、调用、`prompt(tag, body)`、
`shift(tag, k, body)`（可多次调用的组合式续延）与
`shift1(tag, k, body)`（只能进入一次的续延），无可变引用。

## 运行环境

- **Python 3.14.7**（仅使用标准库；3.10+ 亦可运行，目标版本为 3.14.7）
- Linux 原生运行，**安装 Python 后无需任何额外依赖**
- 核心、测试、演示全部离线；不访问网络、不需要账号/密钥/数据库/中间件/
  云 API/Docker

## 快速命令

```bash
# 执行全部必要测试（在仓库根目录）
./.venv/bin/python -m unittest discover -s tests -v

# 固定输入演示：一次性续延的正常消费、别名双调用拒绝、消费时点
./.venv/bin/python demo.py
```

演示真实即时计算（约 0.01 秒，远低于 90 秒预算），无 `sleep`、无预录
结果、不做压力测试。若不使用仓库附带的 venv，等价命令为
`python3.14 -m unittest discover -s tests -v` 与 `python3.14 demo.py`。

## 语言与语义

| 构造 | 含义 |
| --- | --- |
| `Int(n)` | 任意 Python 整数 |
| `Var(x)` | 变量引用 |
| `Add(l, r)` | 整数加法，先求左再求右 |
| `Lam(x, body)` | 单参数词法闭包 |
| `Call(f, a)` | 按值调用，先求函数再求实参 |
| `Prompt(tag, body)` | 为整数 `tag` 设立定界提示并求 body |
| `Shift(tag, k, body)` | 捕获到**最近**同 tag prompt 的求值上下文（句柄可多次调用） |
| `ShiftOnce(tag, k, body)` | 捕获规则同 `Shift`，但句柄**至多进入一次** |

求值规则（按值、左到右）：

1. `Prompt(t, e)`：压入 tag=t 的定界帧后求 `e`；body 正常完成时其值
   就是 prompt 的值。
2. `Shift(t, k, e)` / `ShiftOnce(t, k, e)`：自当前求值点向栈底找
   **最近**的同 tag 定界帧。两种构造的捕获、嵌套异 tag 的最近提示
   规则完全一致。
   - **找到**：该帧与当前点之间的上下文被捕获为不可变续延值并绑定到
     `k`；定界帧保留，`e` 在该 prompt **之内**求值。若 `e` 不调用 `k`，
     被捕获的上下文被放弃——即非局部返回。
   - **找不到**：抛 `UncaughtShiftError`（确定性语义错误）。
3. 调用 `Shift` 产生的 `k(v)` 是**组合式（composable）**恢复：以 `v`
   为洞值驱动捕获的上下文，其外重新包裹一个同 tag 的 prompt，调用点
   本身的上下文完整保留。续延值不可变，**可多次调用**，每次行为一致。
4. 调用 `ShiftOnce` 产生的 `k(v)` 在**原子检查点**消费句柄：
   - 按值语义先求被调用者、再求实参；**实参求值失败（含实参中的
     shift 放弃整个调用）时检查点尚未到达，句柄保持未消费**。
   - 实参得到值之后、拼接任何截获帧之前，原子地“检查并翻转”消费
     状态；此后恢复过程中即使出错（`RuntimeTypeError` 等）或步数
     预算耗尽（`StepLimitError`），消费也**不回滚**，已进入的句柄
     不会复活。
   - 句柄是一等值：经变量别名、lambda 参数任意转手，所有别名共享
     同一个消费单元（语言无可变引用，消费单元是解释器内部状态，
     不向语言暴露赋值操作）。第二次经任意别名进入抛
     `ConsumedContinuationError`，截获的上下文不会重新执行。
5. tag 是显式整数；不同 tag 的提示相互独立、可嵌套。`Shift` 与
   `ShiftOnce` 可在同一程序中混用，新增节点**不改变**原多次续延的
   任何行为。

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
    UncaughtShiftError, ConsumedContinuationError,
    UnboundVariableError, RuntimeTypeError, StepLimitError,
)

r = interpret(Prompt(1, Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5))))))
r.value.display()      # -> "15"（IntVal/CloVal/ContVal/OnceContVal 均有 display()）
r.nodes, r.steps       # 节点数、实际机器步数
r2 = interpret(prog, trace=True)
r2.events              # 求值事件元组（见下）
```

`interpret(program, *, trace=False, max_nodes=1000, max_steps=20000)`
返回 `Result(value, events, nodes, steps)`。

事件（`trace=True`）：

- `prompt_enter/prompt_exit(tag[, 值])`（源码 prompt）、
  `prompt_rewrap_enter/prompt_rewrap_exit`（续延恢复时的重包 prompt，
  普通/一次性首次恢复都会产生）；
- `shift_capture(tag, 捕获帧数)`（Shift）；
- `shift1_capture(tag, 捕获帧数, 截获点 loc)`（ShiftOnce）；
- `cont_call(tag, 帧数)`（普通续延进入）；
- `once_cont_call(tag, 帧数, 状态, 调用点 loc)`：状态为
  `"fresh"`（本次原子消费）或 `"consumed"`（第二次进入被拒绝，随后
  抛 `ConsumedContinuationError`）；
- `add(l, r, s)`、`result(值)`。

事件参数只含 int/str/None 与 `(文件, 行, 列)` 位置元组，全是可打印
基本类型；**不嵌入宿主函数、栈帧或任何可执行对象**。求值失败时，
抛出的 `EvaluationError` 上带 `events` 属性，给出截至错误点的轨迹，
据此核对消费发生在错误之前还是之后（trace=False 时为空元组）。

`ConsumedContinuationError` 另带三个位置字段（节点未带 loc 时为
None）：`capture_loc`（shift1 截获点）、`first_loc`（第一次成功
消费的调用点）、`second_loc`（被拒绝的第二次调用点）。

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

- 构造期（`InvalidProgramError`）：tag 必须是非 bool 的 `int`（`Shift`
  与 `ShiftOnce` 同一规则）；`Int` 拒绝 `bool`、一切 `float`（含
  **NaN/Infinity**）；变量/参数名必须是非空 `str`；子节点必须是
  `Expr`。错误消息附字段名，带 `loc` 的节点还会报告 `文件:行:列`。
- 求值期：`UnboundVariableError`（未绑定变量）、
  `RuntimeTypeError`（对非整数做加法/调用非函数值）、
  `UncaughtShiftError`（shift/shift1 无匹配 prompt）、
  `ConsumedContinuationError`（一次性句柄第二次进入；消息与字段给出
  截获点、首次消费点、被拒绝点）、`StepLimitError`（超步）。
- **消费时点的错误状态**：
  - 实参求值阶段失败 → 句柄未消费（事件轨迹里没有
    `once_cont_call`）；
  - 已进入原子检查点之后失败（恢复途中类型错误、预算耗尽）→ 句柄
    已消费，不回滚；轨迹里先有 `once_cont_call(fresh)` 再有后续事件；
  - 第二次进入 → `ConsumedContinuationError`，截获帧不会被再次拼接。
- 求值无副作用、无部分提交概念；AST 不可变。语言层面的值不可变；
  一次性句柄的消费单元是解释器内部状态，语言中没有暴露任何可变
  引用或复位操作。

## 设计取舍

- **核心是 CEK 风格的显式栈迭代机器**（`delimcc/interpreter.py`）：
  环境是单绑定不可变链表，控制栈是不可变帧元组；全程不使用 Python
  递归，嵌套深度只受步数预算约束。`shift`/`shift1` 通过栈上线性
  查找最近同 tag 帧实现；`k(v)` 通过栈拼接（调用点栈 + 重包 prompt
  帧 + 捕获帧）实现组合式恢复。
- **一次性消费是机器内部的单一原子翻转**：`OnceContVal` 持有一个
  所有别名共享的消费单元；检查只发生在 `callArg`（被调用者与实参
  均已求值完毕）这一个迁移点，先翻转再拼接帧，故“参数失败未消费、
  进入后失败仍消费、预算耗尽不复活、第二次进入被拒绝”都由这一个
  检查点位置推出，而不是多处特判。
- **独立参考实现**（`tests/reference.py`）刻意不导入核心：自带元组
  s-表达式语法，用“显式求值上下文 + 避免捕获替换（alpha 改名）”的
  小步规约机实现同一语义；`shift` 把 k 具体化为可复制的
  `λx. prompt(t, E_t[x])`，`shift1` 则具体化为带机器级共享 id 的
  一次性函数值 `lam1`（消费状态按 id 存于归约机表中），替换复制
  项体不改变 id。两套算法（环境机 vs 替换机、可变单元 vs id 表）
  仅共享题目语义。
- 交叉测试（`tests/test_crosscheck.py`）中期望值只由参考机产生，
  含普通/一次性续延的手写双机对照；另含 120 个固定种子确定性程序
  的双机差分（`tests/program_gen.py`，LCG 固定种子，普通/一次性
  shift 各半生成，仓库内生成、可复现）。一次性特有的四类场景在
  `tests/test_once.py` 中另以手写事件序列逐事件核对。开发期另跑过
  3000 种子差分（非提交测试的一部分）。
- 闭包持有不可变 AST body 的共享引用；因为 AST 不可变且语言无赋值，
  共享不引入别名问题。一次性句柄的可变消费单元不暴露给被求值语言。
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
  errors.py       错误类型体系（含 ConsumedContinuationError）
  values.py       运行时值（IntVal/CloVal/ContVal/OnceContVal）
  interpreter.py  CEK 显式栈解释器（核心算法、原子消费检查点）
tests/
  reference.py    独立参考：显式上下文小步替换机（shift/shift1，不导入核心）
  program_gen.py  确定性程序生成器（固定种子 LCG，shift/shift1 混合）
  test_core.py    原核心语义/校验/边界测试（全部保留）
  test_crosscheck.py  参考机对照与差分、独立性检查、幂等补充
  test_once.py    一次性续延四类场景 + 手写小步事件序列核对
demo.py           固定输入演示（一次性续延消费时点）
```

## 已知限制

- 单参数 lambda；无内置递归绑定（但续延/闭包为一等值）。
- tag 比较为 Python 整数相等；不支持符号 tag。
- 语言中没有异常处理器/可变引用，一次性句柄进入后若恢复失败，失败
  会沿控制栈传播；“进入后已消费”的时点由错误上的事件轨迹证明，
  无法在同一运行内构造“失败后再次调用同一句柄”的源程序（这是语言
  子集的限制，不是消费检查的豁免）。
- 事件与栈帧结构是观察/调试接口，帧内部形状不保证跨版本稳定；
  `OnceContVal.consumed`/`first_call_loc` 为只读观察口，不提供复位。
- 参考机使用替换语义，深嵌套程序中替换成本可能较高（仅用于验收，
  非生产路径；自身带 200000 步保护）。
