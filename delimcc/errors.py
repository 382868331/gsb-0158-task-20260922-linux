"""错误类型体系。

DelimccError
└── InvalidProgramError   构造/校验阶段非法（含节点数超限）
└── EvaluationError       求值阶段错误基类
    ├── UnboundVariableError
    ├── RuntimeTypeError
    ├── UncaughtShiftError   shift 找不到匹配 prompt（无解，非预算问题）
    ├── ContinuationConsumedError  一次性续延被第二次进入（带消费来源位置）
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


class StepLimitError(EvaluationError):
    """求值步数达到预算上限仍未结束（资源预算耗尽）。"""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"求值步数超过上限 {limit}（预算耗尽，非语义无解）")


def _fmt_loc(loc: Optional[tuple]) -> str:
    """把 loc 三元组格式化为 文件:行:列；无位置信息时给出明确说明。"""
    if loc is None:
        return "未知位置"
    return f"{loc[0]}:{loc[1]}:{loc[2]}"


class ContinuationConsumedError(EvaluationError):
    """一次性（one-shot）续延句柄被第二次进入。

    句柄在第一次进入前的原子“检查并消费”点已被标记；此后无论经多少
    变量别名转手，再次调用都在参数求值完成之后、进入截获上下文之前
    被拒绝，不重新执行截获的上下文。

    ``consumed_loc`` 是首次消费该句柄的调用位置（消费来源），
    ``attempt_loc`` 是本次被拒绝的调用位置；均可为 None（无位置信息）。
    """

    def __init__(self, tag: int, consumed_loc: Optional[tuple], attempt_loc: Optional[tuple]):
        self.tag = tag
        self.consumed_loc = consumed_loc
        self.attempt_loc = attempt_loc
        super().__init__(
            f"一次性续延（tag={tag}）只能进入一次："
            f"已在 {_fmt_loc(consumed_loc)} 消费，"
            f"本次进入（{_fmt_loc(attempt_loc)}）被拒绝"
        )
