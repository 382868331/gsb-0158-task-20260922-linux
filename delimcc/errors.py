"""错误类型体系。

DelimccError
└── InvalidProgramError   构造/校验阶段非法（含节点数超限）
└── EvaluationError       求值阶段错误基类
    ├── UnboundVariableError
    ├── RuntimeTypeError
    ├── UncaughtShiftError   shift 找不到匹配 prompt（无解，非预算问题）
    ├── HookViolationError   wind 钩子内捕获/调用续延（违反钩子契约）
    └── StepLimitError       求值步数预算耗尽（资源限制，立即中止不跑钩子）
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


class HookViolationError(EvaluationError):
    """wind 的钩子（before/after）内捕获或调用了续延。

    契约要求钩子只记录事件：钩子里的游离 ``shift``（无论是否存在匹配
    prompt）与对续延值的调用都在此报错；钩子里的其他求值错误
    （未绑定变量、类型错误等）仍按原类型抛出。
    """

    def __init__(self, where: str, detail: str):
        self.where = where
        super().__init__(f"wind 的 {where} 钩子里不允许 {detail}")


class StepLimitError(EvaluationError):
    """求值步数达到预算上限仍未结束（资源预算耗尽）。"""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"求值步数超过上限 {limit}（预算耗尽，非语义无解）")
