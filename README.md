# delimcc：多重分隔提示的定界续延解释器

一个零依赖的 Python 题库，实现带多 tag 分隔提示（delimited control）的
AST 解释器：整数、加法、变量、lambda、调用、`prompt(tag, body)` 与
`shift(tag, k, body)`，无可变引用。

## 运行环境

- **Python 3.14.7**（仅使用标准库；3.10+ 亦可运行，目标版本为 3.14.7）
- Linux 原生运行，**安装 Python 后无需任何额外依赖**
- 核心、测试、演示全部离线；不访问网络、不需要账号/密钥/数据库/中间件/
  云 API/Docker

## 快速命令

```bash
# 执行全部必要测试（在仓库根目录）
python -m unittest discover -s tests -v

# 固定输入演示：一个正常结果（非局部返回）+ 实际触发的拒绝/异常边界
python demo.py
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
| `Shift(tag, k, body)` | 捕获到**最近**同 tag prompt 的求值上下文 |

求值规则（按值、左到右）：

1. `Prompt(t, e)`：压入 tag=t 的定界帧后求 `e`；body 正常完成时其值
   就是 prompt 的值。
2. `Shift(t, k, e)`：自当前求值点向栈底找最近的同 tag 定界帧。
   - **找到**：该帧与当前点之间的上下文被捕获为不可变续延值并绑定到
     `k`；定界帧保留，`e` 在该 prompt **之内**求值。若 `e` 不调用 `k`，
     被捕获的上下文被放弃——即非局部返回。
   - **找不到**：抛 `UncaughtShiftError`（确定性语义错误）。
3. 调用 `k(v)` 是**组合式（composable）**恢复：以 `v` 为洞值驱动捕获
   的上下文，其外重新包裹一个同 tag 的 prompt，调用点本身的上下文完整
   保留。续延值不可变，**可多次调用**，每次行为一致。
4. tag 是显式整数；不同 tag 的提示相互独立、可嵌套。

## 公开接口

```python
from delimcc import (
    # AST
    Int, Var, Add, Lam, Call, Prompt, Shift,
    # 求值
    interpret, Event, Result,
    # 错误
    DelimccError, InvalidProgramError, EvaluationError,
    UncaughtShiftError, UnboundVariableError, RuntimeTypeError, StepLimitError,
)

r = interpret(Prompt(1, Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5))))))
r.value.display()      # -> "15"（IntVal/CloVal/ContVal 均有 display()）
r.nodes, r.steps       # 节点数、实际机器步数
r2 = interpret(prog, trace=True)
r2.events              # 求值事件元组（见下）
```

`interpret(program, *, trace=False, max_nodes=1000, max_steps=20000)`
返回 `Result(value, events, nodes, steps)`。

事件（`trace=True`）：`prompt_enter/prompt_exit(tag[, 值])`（源码
prompt）、`prompt_rewrap_enter/prompt_rewrap_exit`（`k(v)` 恢复时的重包
prompt）、`shift_capture(tag, 捕获帧数)`、`cont_call(tag, 帧数)`、
`add(l, r, s)`、`result(值)`。

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
  `UncaughtShiftError`（无匹配 prompt）、`StepLimitError`（超步）。
- 求值无副作用、无部分提交概念；AST 与运行时值均不可变，失败不留
  未说明的状态变更。

## 设计取舍

- **核心是 CEK 风格的显式栈迭代机器**（`delimcc/interpreter.py`）：
  环境是单绑定不可变链表，控制栈是不可变帧元组；全程不使用 Python
  递归，嵌套深度只受步数预算约束。`shift` 通过栈上线性查找最近同 tag
  帧实现；`k(v)` 通过栈拼接（调用点栈 + 重包 prompt 帧 + 捕获帧）实现
  组合式恢复。
- **独立参考实现**（`tests/reference.py`）刻意不导入核心：自带元组
  s-表达式语法，用“显式求值上下文 + 避免捕获替换（alpha 改名）”的
  小步规约机实现同一语义；`shift` 把 k 具体化为
  `λx. prompt(t, E_t[x])`，续延即可复制的上下文快照。两套算法仅共享
  题目语义。
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
  ast.py          不可变 AST 与构造期校验
  errors.py       错误类型体系
  values.py       运行时值（IntVal/CloVal/ContVal）
  interpreter.py  CEK 显式栈解释器（核心算法）
tests/
  reference.py    独立参考：显式上下文小步替换机（不导入核心）
  program_gen.py  确定性程序生成器（固定种子 LCG）
  test_core.py    核心语义/校验/边界测试
  test_crosscheck.py  参考机对照与差分、独立性检查、幂等补充
demo.py           固定输入演示
```

## 已知限制

- 单参数 lambda；无内置递归绑定（但续延/闭包为一等值）。
- tag 比较为 Python 整数相等；不支持符号 tag。
- 事件与栈帧结构是观察/调试接口，帧内部形状不保证跨版本稳定。
- 参考机使用替换语义，深嵌套程序中替换成本可能较高（仅用于验收，
  非生产路径；自身带 200000 步保护）。
