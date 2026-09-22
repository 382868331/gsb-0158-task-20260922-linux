# delimcc — 多重分隔提示的定界续延解释器

一个从零实现的 AST 解释器，语言支持**整数、加法、变量、单参数 lambda、
函数调用，以及多 tag 的 `prompt` / `shift` 定界续延**（无可变引用）。
另附一份**独立的显式求值上下文小步参考实现**作为测试预言机。

- **Python 版本**：CPython 3.14.7（使用 3.10+ 语法，标准库）
- **依赖**：无第三方依赖，安装 Python 后即可运行；完全离线，不访问网络、
  账号、密钥、数据库或任何外部服务。
- **平台**：Linux 原生运行。

## 运行

```bash
python -m unittest discover -s tests -v   # 必要测试（47 个）
python demo.py                            # 约毫秒级，非局部返回演示 + 异常边界
```

## 语言与语义

```
e ::= Int(value)                         整数（Python 任意精度 int，精确）
    | Add(left, right)                  整数加法
    | Var(name)                         变量
    | Lam(param, body)                  单参数 lambda（词法闭包）
    | Call(fn, arg)                     调用
    | Prompt(tag, body)                 tag 为显式整数的定界提示
    | Shift(tag, param, body)           捕获到最近同 tag 提示的续延
```

求值策略：**按值调用、从左到右**。

- `Prompt(tag, body)`：压入带 `tag` 的 prompt 帧并求值 `body`；值正常穿过
  该帧返回时帧被移除。
- `Shift(tag, k, body)`：自栈顶向下寻找**最近的同 tag prompt 帧**。该帧
  之上的求值上下文被物化为续延值 `k`；被捕获帧与匹配的 prompt 帧一并移除；
  `body` 在一个**同 tag 的新 prompt 内**求值。多次调用 `k` 均可（多拍
  multi-shot：捕获的帧是复制的，不被消耗）。
- 调用 `k(v)`：压入同 tag 的新 prompt 帧，再按原顺序重放被捕获帧，`v`
  流入最内层帧。于是穿过恢复上下文再次执行的同 tag `shift` 仍能被捕获。
- 若沿续延找不到同 tag prompt，则报 `NoPromptError`——这是**证明无解**，
  与 `StepLimitError`（搜索/求值预算耗尽，并非无解证明）严格区分。

## 公开接口

```python
from delimcc import (
    Int, Add, Var, Lam, Call, Prompt, Shift,   # AST 节点（frozen dataclass）
    evaluate,                                   # 主入口
    describe_value,
    DlmccError, BadAstError, NodeLimitError,
    StepLimitError, NoPromptError, UnboundVarError,
)

result = evaluate(program)          # validate + 求值
result.as_int()                     # 整数结果（否则 BadAstError）
result.value                        # IntVal | Closure | ContVal
result.events                       # (kind, tag, step) 事件元组
result.steps                        # 实际 CEK 步数
result.node_count                   # 校验后的 AST 节点数

evaluate(program, max_steps=20000, max_nodes=1000, trace=False)
```

事件 `kind`：`prompt-enter` / `prompt-exit` / `shift` / `resume`。

独立参考实现（**不调用被测核心**，基于项代换 + 显式上下文小步规约）：

```python
from delimcc.reference import reduce_program
r = reduce_program(program)        # RefResult(value, steps, events)
```

## 输入上限（接口边界）

| 边界 | 默认值 | 超限错误 |
|---|---|---|
| 单个程序 AST 节点数 | **1000** | `NodeLimitError` (code=`node-limit`) |
| 单次求值 CEK 转移步数 | **20000** | `StepLimitError` (code=`step-limit`) |

两者均在求值**前/中**强制；短演示不穷举这些规模。预算参数本身必须是
正整数（拒绝 `bool`）。

## 错误语义

所有错误继承 `DlmccError`，带机器可读 `code` 与可定位的 `loc`
（从根开始的属性路径，如 `program > Add.right > Prompt.tag`）：

- **数值输入拒绝 NaN/Infinity**：`Int`/`tag` 只接受真正的 `int`；
  `float('nan')`、`±inf`、普通浮点（如 `1.999`）一律拒绝，**不做静默
  舍入纠正**。
- **整数参数拒绝 bool**：尽管 Python 的 `bool` 是 `int` 子类，`True/False`
  作为 `value` 或 `tag` 都被拒绝。
- 名称必须是非空 `str`；子字段必须是 AST 节点；变量未绑定报
  `UnboundVarError`；`+` 两侧必须是整数；只能调用闭包或续延值。
- 求值是无状态的纯函数（环境为不可变父链），不存在“失败后留下部分
  变更”的有状态批次。

## 设计取舍

1. **核心用 CEK 抽象机**（`delimcc/core.py`）：显式栈帧、显式步数，便于
   执行 20000 步预算并发射事件；不使用 Python 异常/生成器去实现控制效果
   本身（异常只用于报错），因此语义可审计、续延可多拍重放。
2. **参考实现独立**（`delimcc/reference.py`）：把求值上下文显式表示为帧
   列表，每步“分解为 E[r] → 重写 r → 回填”，续延是上下文数据 `K_tag(F)`，
   多拍来自**避免捕获的代换对 F 的复制**。它不 import 核心、不与核心共享
   运行时值；测试期望值只来自它。两实现的“往返一致”只是补充交叉验证。
3. 整数为 Python 任意精度整数，**所有结果精确，无浮点运算，因此不存在
   浮点误差口径**（README 据实说明：本语言无数值近似）。
4. tag 模型为**多提示（multi-prompt）**：不同整数 tag 互不可见；最近同
   tag 原则意味着内层同 tag prompt 会遮蔽外层。
5. 续延体内的 `k` 是普通一等值：可作参数传递、可被调用任意次；但本语言
   没有可变引用，故 `k` 无法在调用后“记住”状态。

## 已知限制 / 范围声明

- 这是题面契约定义的**有限数学子集**：无递归绑定（无名字自引用）、无
  布尔/字符串/浮点、无元组/记录、无 IO、无可变状态；不宣称兼容任何行业
  标准语言或完整控制效果演算。
- 无原生递归意味着不能靠语言自身表达无限循环；步预算主要约束多拍续延
  造成的规模放大。
- 参考实现采用代换，深层生成程序上其步计数与 CEK 不同（测试只比较最终值
  与事件序列，不比较步数）。
- 闭包最终值以结构描述符比较（参数名），不做 alpha 等价归一。

## 仓库结构

```
delimcc/
  __init__.py     公开 API
  ast.py          AST 节点、递归校验、节点计数
  core.py         CEK 机（被测核心）
  reference.py    独立小步参考实现（测试预言机）
  errors.py       错误类型与 code/loc 语义
tests/
  test_basic.py       整数/加法/lambda/调用
  test_delimcc.py     嵌套异 tag、重复 k、捕获加法右侧、无匹配提示等
  test_reference.py   与独立参考的交叉验证（含 200 个确定性生成程序）
  test_validation.py  NaN/Infinity/bool 拒绝、loc、1000/20000 边界
demo.py             固定输入：非局部返回 + 实际触发的 NoPromptError
```
