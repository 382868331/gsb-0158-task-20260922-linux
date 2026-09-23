# delimcc：多重分隔提示的定界续延解释器

一个零依赖的 Python 题库，实现带多 tag 分隔提示（delimited control）的
AST 解释器：整数、加法、变量、lambda、调用、`prompt(tag, body)`、
`shift(tag, k, body)` 与 `wind(before, body, after)` 作用域钩子，
无可变引用。

## 运行环境

- **Python 3.14.7**（仅使用标准库；3.10+ 亦可运行，目标版本为 3.14.7）
- Linux 原生运行，**安装 Python 后无需任何额外依赖**
- 核心、测试、演示全部离线；不访问网络、不需要账号/密钥/数据库/中间件/
  云 API/Docker

## 快速命令

```bash
# 执行全部必要测试（在仓库根目录）
python -m unittest discover -s tests -v

# 固定输入演示：wind 正常/跨作用域恢复/实际错误 + 旧能力非局部返回
python demo.py
```

演示真实即时计算（约 0.01 秒，远低于 90 秒预算），无 `sleep`、无预录
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
| `Wind(before, body, after)` | 动态作用域钩子：进入求 `before`，离开求 `after` |

求值规则（按值、左到右）：

1. `Prompt(t, e)`：压入 tag=t 的定界帧后求 `e`；body 正常完成时其值
   就是 prompt 的值。
2. `Shift(t, k, e)`：自当前求值点向栈底找最近的同 tag 定界帧。
   - **找到**：该帧与当前点之间的上下文被捕获为不可变续延值并绑定到
     `k`；定界帧保留，`e` 在该 prompt **之内**求值。若 `e` 不调用 `k`，
     被捕获的上下文被放弃——即非局部返回。
   - **找不到**：抛 `UncaughtShiftError`（确定性语义错误），并按异常
     退出执行沿途激活作用域的钩子。
3. 调用 `k(v)` 是**组合式（composable）**恢复：以 `v` 为洞值驱动捕获
   的上下文，其外重新包裹一个同 tag 的 prompt，调用点本身的上下文完整
   保留。续延值不可变，**可多次调用**，每次行为一致；被一起捕获的
   异 tag prompt 与 wind 作用域在每次恢复时都重新建立。
4. tag 是显式整数；不同 tag 的提示相互独立、可嵌套。

### Wind 作用域钩子

`Wind(before, body, after)` 表达一个带进入/离开钩子的动态作用域。
`before`/`after` 是普通表达式，其**结果值被丢弃**，只用于通过求值
事件观察进入/离开；整个 Wind 的值取 `body` 的值。钩子在三种情形下
按作用域的离开/进入执行：

- **正常返回**：进入先求 `before`；body 正常完成后求 `after`。
- **异常退出**：求值错误（类型错、未绑定变量、无匹配 prompt 等）向外
  传播穿过仍激活的作用域时，自内向外逐个求其 `after`，随后抛出原
  错误；`before` 自身失败时该作用域视为**未进入**，不执行其 `after`；
  若某个 error 钩子自身失败，新错误取代原错误继续向外展开。
- **续延捕获与恢复**：`shift` 捕获跨越激活作用域时，这些作用域随捕获
  一起**离开**（提交捕获后自内向外求 `after`）；`k(v)` 恢复时它们
  **重新进入**（驱动洞值前自外向内求 `before`），洞值穿过作用域再次
  正常离开时求 `after`。多次恢复每次都完整重放。

**钩子不捕获续延**（本次迭代的明确限定）：`before`/`after` 的 AST 中
**任何位置**（含 lambda 体内）静态出现 `Shift` 都在构造期抛
`InvalidProgramError`；静态检查无法覆盖的情形——钩子里动态调用了
词法环境中体内含 shift 的闭包——在求值期抛 `HookShiftError`。

> 步数预算耗尽（`StepLimitError`）是资源终止而非语言内异常，**不**
> 触发 error 钩子。

## 公开接口

```python
from delimcc import (
    # AST
    Int, Var, Add, Lam, Call, Prompt, Shift, Wind,
    # 求值
    interpret, Event, Result,
    # 错误
    DelimccError, InvalidProgramError, EvaluationError,
    UncaughtShiftError, HookShiftError, UnboundVariableError,
    RuntimeTypeError, StepLimitError,
)

r = interpret(Prompt(1, Add(Int(10), Shift(1, "k", Call(Var("k"), Int(5))))))
r.value.display()      # -> "15"（IntVal/CloVal/ContVal 均有 display()）
r.nodes, r.steps       # 节点数、实际机器步数
r2 = interpret(prog, trace=True)
r2.events              # 求值事件元组（见下）
```

`interpret(program, *, trace=False, max_nodes=1000, max_steps=20000)`
返回 `Result(value, events, nodes, steps)`。`trace=True` 时求值失败所抛
`EvaluationError` 的 `events` 属性同样携带截至失败点的事件轨迹
（`trace=False` 时为 `()`）。

事件（`trace=True`）：

- prompt：`prompt_enter/prompt_exit(tag[, 值])`（源码 prompt）、
  `prompt_rewrap_enter/prompt_rewrap_exit`（`k(v)` 恢复时重包及被一起
  捕获 prompt 的重建）。
- 续延：`shift_capture(tag, 捕获帧数)`、`cont_call(tag, 帧数)`。
- 作用域钩子：`wind_before(id, why)`、`wind_after(id, why)`。
  - `wind_before` 的 why：`enter`（正常进入）、`resume`（恢复重入）。
  - `wind_after` 的 why：`normal`（正常返回离开）、`capture`（被 shift
    捕获带走）、`error`（异常退出）。
  - 同一作用域实例在正常进入、被捕获与每次恢复时使用相同 `id`。
- 其他：`add(l, r, s)`、`result(值)`。

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
  必须是非空 `str`；子节点必须是 `Expr`。`Wind` 的 `before`/`after`
  钩子中任意位置（含 lambda 体内）出现 `Shift` 同样在构造期拒绝。
  错误消息附字段名，带 `loc` 的节点还会报告 `文件:行:列`。
- 求值期：`UnboundVariableError`（未绑定变量）、
  `RuntimeTypeError`（对非整数做加法/调用非函数值）、
  `UncaughtShiftError`（无匹配 prompt）、
  `HookShiftError`（钩子表达式中动态触发了 shift 捕获）、
  `StepLimitError`（超步）。求值错误穿过激活 wind 作用域时先执行其
  error 钩子；error 钩子失败则以新错误取代。
- 求值对 AST/运行时值无写入、无部分提交；捕获提交前钩子失败则续延不
  生成，失败不留未说明的状态变更（`trace=True` 时仅错误对象上多一个
  只读 `events` 观察轨迹）。

## 设计取舍

- **核心是 CEK 风格的显式栈迭代机器**（`delimcc/interpreter.py`）：
  环境是单绑定不可变链表，控制栈是不可变帧元组；全程不使用 Python
  递归，嵌套深度只受步数预算约束。`shift` 通过栈上线性查找最近同 tag
  帧实现；`k(v)` 通过栈拼接（调用点栈 + 重包 prompt 帧 + 捕获帧）实现
  组合式恢复。`wind` 用进入帧/激活帧/边界帧表达作用域：捕获把跨越的
  激活帧转为边界帧并先自内向外跑离开钩子；恢复时按捕获帧外→内统一
  “重放进入序列”（交错重入 wind 与重建被捕获 prompt），洞值穿过边界
  帧再跑离开钩子。
- **独立参考实现**（`tests/reference.py`）刻意不导入核心：自带元组
  s-表达式语法，用“显式求值上下文 + 避免捕获替换（alpha 改名）”的
  小步规约机实现同一语义；`shift` 把 k 具体化为
  `λx. prompt(t, E_t[x])`，跨作用域处重建为 windresume 上下文，续延即
  可复制的上下文快照；`run_trace` 额外产出与核心同名同参的事件序列。
  两套算法仅共享题目语义。
- 交叉测试（`tests/test_crosscheck.py`、`tests/test_wind.py`）中期望值
  只由参考机产生：既比对最终值/错误类别，也对含 wind 的成功程序逐事件
  比对完整轨迹（作用域 id 各自计数，比对前归一化）。另含 120 个固定
  种子确定性程序的双机差分（`tests/program_gen.py`，LCG 固定种子）。
  开发期另跑过 2000 种子（753 条含 wind 完整轨迹）零分歧（非提交测试
  的一部分）。
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
  test_wind.py    wind 钩子：手算事件序列 + 完整轨迹双机差分
demo.py           固定输入演示
```

## 已知限制

- 单参数 lambda；无内置递归绑定（但续延/闭包为一等值）。
- tag 比较为 Python 整数相等；不支持符号 tag。
- 事件与栈帧结构是观察/调试接口，帧内部形状不保证跨版本稳定。
- 本次 wind 迭代按题面限定交付：钩子只记录事件、不捕获续延；不实现
  钩子自身重入（钩子中触发 shift 直接报错）、资源句柄式生命周期管理，
  也不为“多次恢复”规定额外的可观察语义（多次恢复即重复重放同一不可
  变快照，行为一致）。
- 参考机使用替换语义，深嵌套程序中替换成本可能较高（仅用于验收，
  非生产路径；自身带 200000 步保护）。
