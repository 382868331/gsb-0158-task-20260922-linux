"""错误类型体系。

DelimccError
└── InvalidProgramError   构造/校验阶段非法（含节点数超限）
└── EvaluationError       求值阶段错误基类
    ├── UnboundVariableError
    ├── RuntimeTypeError
    ├── UncaughtShiftError   shift 找不到匹配 prompt（无解，非预算问题）
    ├── HookShiftError       钩子表达式中动态触发 shift 捕获续延
    └── StepLimitError       求值步数预算耗尽（资源限制，与语义无解区分）
"""

from __future__ import annotations

from typing import Optional


class DelimccError(Exception):
    """本库所有错误的基类。"""


class InvalidProgramError(DelimccError, ValueError):
    """程序在求值前的构造/校验阶段即非法。

    包括：节点字段类型错误、tag 非 int、bool 冒充整数、NaN/Infinity、
    节点总数超过上限等。``loc`` 给出可定位信息（可为 None）。
    """

    def __init__(self, message: str, loc: Optional[tuple] = None):
        self.loc = loc
        text = message
        if loc is not None:
            text = f"{message} (位置 {loc[0]}:{loc[1]}:{loc[2]})"
        super().__init__(text)


class EvaluationError(DelimccError):
    """求值阶段错误的基类。"""


class UnboundVariableError(EvaluationError):
    """引用了环境中不存在的变量。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"未绑定的变量: {name!r}")


class RuntimeTypeError(EvaluationError):
    """运行时值的种类与运算要求不符（如对函数做加法）。"""


class UncaughtShiftError(EvaluationError):
    """shift 找不到同 tag 的包围 prompt。

    这是确定性的语义错误（证明无解），与步数预算耗尽严格区分。
    """

    def __init__(self, tag: int):
        self.tag = tag
        super().__init__(f"shift 找不到 tag={tag} 的包围 prompt")


class HookShiftError(EvaluationError):
    """在 Wind 钩子表达式求值期间动态触发了 shift 捕获。

    钩子 AST 中静态出现的 Shift 在构造期即被
    ``InvalidProgramError`` 拒绝；本错误对应静态检查无法覆盖的情形：
    钩子表达式调用了词法环境中其体内含 shift 的闭包。契约规定钩子
    只记录事件、不捕获续延，故这是确定性的求值期语义错误，触发后按
    异常退出处理（外层仍激活作用域的 error 钩子照常执行）。
    """

    def __init__(self, tag: int, phase: str):
        self.tag = tag
        self.phase = phase
        super().__init__(
            f"wind 钩子（{phase}）中不允许触发 shift 捕获（tag={tag}）："
            "钩子只记录事件，不捕获续延"
        )


class StepLimitError(EvaluationError):
    """求值步数达到预算上限仍未结束（资源预算耗尽）。"""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"求值步数超过上限 {limit}（预算耗尽，非语义无解）")
