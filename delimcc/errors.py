"""错误类型体系。

DelimccError
└── InvalidProgramError   构造/校验阶段非法（含节点数超限）
└── EvaluationError       求值阶段错误基类
    ├── UnboundVariableError
    ├── RuntimeTypeError
    ├── UncaughtShiftError   shift 找不到匹配 prompt（无解，非预算问题）
    ├── OneShotContinuationError   一次性续延错误基类
    │   └── ContinuationConsumedError  一次性续延被第二次恢复
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


class OneShotContinuationError(EvaluationError):
    """一次性续延（shift_once 捕获的续延）相关错误的基类。"""


class ContinuationConsumedError(OneShotContinuationError):
    """一次性续延在被消费后再次被恢复。

    每个 shift_once 捕获产生唯一的一次性续延对象；该对象无论通过哪个
    变量/别名访问，全程序只能成功恢复一次。消费在参数求值成功之后、
    恢复开始之前的同一机器迁移内原子置位，故参数求值失败时续延仍未
    消费，而恢复开始后即使被恢复的上下文自身抛错，续延也已消费。
    """

    def __init__(self, tag: int):
        self.tag = tag
        super().__init__(
            f"一次性续延已被消费，不能第二次恢复（tag={tag}）"
        )


class StepLimitError(EvaluationError):
    """求值步数达到预算上限仍未结束（资源预算耗尽）。"""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"求值步数超过上限 {limit}（预算耗尽，非语义无解）")
