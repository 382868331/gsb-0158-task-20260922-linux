"""错误类型体系。

DelimccError
└── InvalidProgramError   构造/校验阶段非法（含节点数超限）
└── EvaluationError       求值阶段错误基类
    ├── UnboundVariableError
    ├── RuntimeTypeError
    ├── UncaughtShiftError   shift/shift1 找不到匹配 prompt（无解，非预算问题）
    ├── ConsumedContinuationError  一次性续延被第二次进入（句柄已消费）
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
    """求值阶段错误的基类。

    解释器抛出时会在实例上挂 ``events``：``interpret(..., trace=True)``
    时为截至错误发生时的事件轨迹，否则为空元组。用于在失败路径上观察
    一次性续延的消费时点（消费发生在抛错之前还是之后）。
    """

    #: 类型上声明默认值；实际轨迹由解释器在抛出前挂上。
    events: tuple = ()


class UnboundVariableError(EvaluationError):
    """引用了环境中不存在的变量。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"未绑定的变量: {name!r}")


class RuntimeTypeError(EvaluationError):
    """运行时值的种类与运算要求不符（如对函数做加法）。"""


class UncaughtShiftError(EvaluationError):
    """shift/shift1 找不到同 tag 的包围 prompt。

    这是确定性的语义错误（证明无解），与步数预算耗尽严格区分。
    """

    def __init__(self, tag: int):
        self.tag = tag
        super().__init__(f"shift 找不到 tag={tag} 的包围 prompt")


class ConsumedContinuationError(EvaluationError):
    """一次性续延（shift1 捕获的句柄）在首次消费之后被再次进入。

    句柄经任意变量别名转手共享同一个消费状态：第二次调用（无论来自
    哪个别名、嵌套多深）都在“实参求值完成之后、进入续延之前”的原子
    检查点被拒绝，截获的上下文不会重新执行。

    位置字段均可能为 None（对应节点未带 loc）：
      ``capture_loc``  shift1 的截获点
      ``first_loc``    第一次（已成功消费）调用点
      ``second_loc``   被拒绝的第二次调用点
    """

    def __init__(
        self,
        tag: int,
        capture_loc: Optional[tuple] = None,
        first_loc: Optional[tuple] = None,
        second_loc: Optional[tuple] = None,
    ):
        self.tag = tag
        self.capture_loc = capture_loc
        self.first_loc = first_loc
        self.second_loc = second_loc
        head = f"一次性续延 tag={tag} 已被消费，不能再次进入"
        details = []
        if first_loc is not None:
            details.append(f"首次消费位置 {first_loc[0]}:{first_loc[1]}:{first_loc[2]}")
        if second_loc is not None:
            details.append(f"被拒绝调用位置 {second_loc[0]}:{second_loc[1]}:{second_loc[2]}")
        if capture_loc is not None:
            details.append(f"截获位置 {capture_loc[0]}:{capture_loc[1]}:{capture_loc[2]}")
        super().__init__(head + "（" + "；".join(details) + "）" if details else head)


class StepLimitError(EvaluationError):
    """求值步数达到预算上限仍未结束（资源预算耗尽）。"""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"求值步数超过上限 {limit}（预算耗尽，非语义无解）")
