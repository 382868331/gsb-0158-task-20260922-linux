"""抽象语法树（不可变数据结构）。

节点类型
--------
Int(value)                      整数字面量
Var(name)                       变量
Add(left, right)                整数加法
Lam(param, body)                单参数 lambda
Call(fn, arg)                   函数调用
Prompt(tag, body)               定界提示，tag 为显式 int
Shift(tag, param, body)         捕获到最近同 tag prompt 的求值上下文

Ref(init)                       分配可变单元（机器 store 中的身份值）
Deref(ref)                      读取单元内容
Assign(ref, value)              写入单元，返回写入的值
Choose(options)                 有限多选效果：显式非负个候选表达式
Fail()                          失败效果：结束当前搜索分支

节点可选带位置信息 loc: 形如 ``("<文件>", 行, 列)`` 的三元组，仅用于
错误定位；库自身构造的节点 loc 为 None。

任何构造入口都会做完整校验：tag 必须是非 bool 的 int；数值输入拒绝
float（含 NaN/Infinity）；name/param 必须是非空 str。非法构造立即抛出
``InvalidProgramError``（含节点位置），不做静默纠正。
"""

from __future__ import annotations

from typing import Optional, Tuple, Union

from .errors import InvalidProgramError

Loc = Optional[Tuple[str, int, int]]

_VALID = {
    "Int", "Var", "Add", "Lam", "Call", "Prompt", "Shift",
    "Ref", "Deref", "Assign", "Choose", "Fail",
}


def _is_int(x: object) -> bool:
    # bool 是 int 的子类，按契约显式拒绝。
    return isinstance(x, int) and not isinstance(x, bool)


def _check_tag(tag: object, loc: Loc) -> int:
    if not _is_int(tag):
        kind = "bool" if isinstance(tag, bool) else type(tag).__name__
        raise InvalidProgramError(
            f"prompt/shift 的 tag 必须是 int（显式整数），得到 {kind}: {tag!r}",
            loc,
        )
    return tag  # type: ignore[return-value]


def _check_name(name: object, what: str, loc: Loc) -> str:
    if not isinstance(name, str):
        raise InvalidProgramError(
            f"{what} 必须是 str，得到 {type(name).__name__}: {name!r}", loc
        )
    if not name:
        raise InvalidProgramError(f"{what} 不能为空字符串", loc)
    return name


class Expr:
    """所有 AST 节点的基类。节点不可变。"""

    __slots__ = ("loc",)

    #: 节点种类名，供 switch/序列化使用。
    kind: str = ""

    def __init__(self, loc: Loc = None):
        self.loc = loc

    # --- 结构遍历 -------------------------------------------------------
    def children(self) -> Tuple["Expr", ...]:
        """按求值顺序返回直接子表达式。"""
        raise NotImplementedError

    def node_count(self) -> int:
        """统计整棵树的节点数（含自身）；迭代遍历，深树不触发递归限制。"""
        n = 0
        todo = [self]
        while todo:
            e = todo.pop()
            n += 1
            todo.extend(e.children())
        return n

    def with_loc(self, loc: Loc) -> "Expr":
        """返回带位置信息的副本（不修改原节点）。"""
        raise NotImplementedError

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Expr) or self.kind != other.kind:
            return NotImplemented
        return tuple(_fields(self)) == tuple(_fields(other))

    def __hash__(self) -> int:
        return hash((self.kind, tuple(_fields(self))))

    def __repr__(self) -> str:
        args = ", ".join(repr(v) for v in _fields(self))
        return f"{self.kind}({args})"


def _fields(e: Expr) -> Tuple[object, ...]:
    k = e.kind
    if k == "Int":
        return (e.value,)  # type: ignore[attr-defined]
    if k == "Var":
        return (e.name,)  # type: ignore[attr-defined]
    if k == "Add":
        return (e.left, e.right)  # type: ignore[attr-defined]
    if k == "Lam":
        return (e.param, e.body)  # type: ignore[attr-defined]
    if k == "Call":
        return (e.fn, e.arg)  # type: ignore[attr-defined]
    if k == "Prompt":
        return (e.tag, e.body)  # type: ignore[attr-defined]
    if k == "Shift":
        return (e.tag, e.param, e.body)  # type: ignore[attr-defined]
    if k == "Ref":
        return (e.init,)  # type: ignore[attr-defined]
    if k == "Deref":
        return (e.ref,)  # type: ignore[attr-defined]
    if k == "Assign":
        return (e.ref, e.value)  # type: ignore[attr-defined]
    if k == "Choose":
        return tuple(e.options)  # type: ignore[attr-defined]
    if k == "Fail":
        return ()
    raise InvalidProgramError(f"未知节点种类: {k!r}")


class Int(Expr):
    """整数字面量。"""

    __slots__ = ("value",)
    kind = "Int"

    def __init__(self, value: int, loc: Loc = None):
        super().__init__(loc)
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float):
                # 明确指出 NaN/Infinity，不与普通 float 混淆。
                import math

                why = "NaN/Infinity 等非有限数" if not math.isfinite(value) else "浮点数"
                got = f"float（{why}）"
            else:
                got = "bool" if isinstance(value, bool) else type(value).__name__
            raise InvalidProgramError(
                f"Int 的值必须是 int（拒绝 bool、float、NaN/Infinity），得到 {got}: {value!r}",
                loc,
            )
        self.value = value

    def children(self) -> Tuple["Expr", ...]:
        return ()

    def with_loc(self, loc: Loc) -> "Int":
        return Int(self.value, loc)


class Var(Expr):
    """变量引用。"""

    __slots__ = ("name",)
    kind = "Var"

    def __init__(self, name: str, loc: Loc = None):
        super().__init__(loc)
        self.name = _check_name(name, "变量名", loc)

    def children(self) -> Tuple["Expr", ...]:
        return ()

    def with_loc(self, loc: Loc) -> "Var":
        return Var(self.name, loc)


class Add(Expr):
    """left + right，两侧都必须求值为整数。"""

    __slots__ = ("left", "right")
    kind = "Add"

    def __init__(self, left: Expr, right: Expr, loc: Loc = None):
        super().__init__(loc)
        self.left = _check_child(left, "Add.left", loc)
        self.right = _check_child(right, "Add.right", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.left, self.right)

    def with_loc(self, loc: Loc) -> "Add":
        return Add(self.left, self.right, loc)


class Lam(Expr):
    """lambda: ``(param) => body``。词法闭包，无可变状态。"""

    __slots__ = ("param", "body")
    kind = "Lam"

    def __init__(self, param: str, body: Expr, loc: Loc = None):
        super().__init__(loc)
        self.param = _check_name(param, "lambda 参数名", loc)
        self.body = _check_child(body, "Lam.body", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.body,)

    def with_loc(self, loc: Loc) -> "Lam":
        return Lam(self.param, self.body, loc)


class Call(Expr):
    """fn(arg)，按值左到右：先求 fn 再求 arg。"""

    __slots__ = ("fn", "arg")
    kind = "Call"

    def __init__(self, fn: Expr, arg: Expr, loc: Loc = None):
        super().__init__(loc)
        self.fn = _check_child(fn, "Call.fn", loc)
        self.arg = _check_child(arg, "Call.arg", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.fn, self.arg)

    def with_loc(self, loc: Loc) -> "Call":
        return Call(self.fn, self.arg, loc)


class Prompt(Expr):
    """prompt(tag, body)：为 tag 设立定界提示。"""

    __slots__ = ("tag", "body")
    kind = "Prompt"

    def __init__(self, tag: int, body: Expr, loc: Loc = None):
        super().__init__(loc)
        self.tag = _check_tag(tag, loc)
        self.body = _check_child(body, "Prompt.body", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.body,)

    def with_loc(self, loc: Loc) -> "Prompt":
        return Prompt(self.tag, self.body, loc)


class Shift(Expr):
    """shift(tag, k, body)：捕获到最近同 tag prompt 的上下文并绑定为 k。"""

    __slots__ = ("tag", "param", "body")
    kind = "Shift"

    def __init__(self, tag: int, param: str, body: Expr, loc: Loc = None):
        super().__init__(loc)
        self.tag = _check_tag(tag, loc)
        self.param = _check_name(param, "shift 续延参数名", loc)
        self.body = _check_child(body, "Shift.body", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.body,)

    def with_loc(self, loc: Loc) -> "Shift":
        return Shift(self.tag, self.param, self.body, loc)


class Ref(Expr):
    """分配一个可变单元，初值为 init 的求值结果。

    单元身份是一个不透明 :class:`~delimcc.values.RefVal`；内容保存在
    机器 store 中。搜索在 choose 分叉点快照 store，各分支写入相互隔离。
    """

    __slots__ = ("init",)
    kind = "Ref"

    def __init__(self, init: Expr, loc: Loc = None):
        super().__init__(loc)
        self.init = _check_child(init, "Ref.init", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.init,)

    def with_loc(self, loc: Loc) -> "Ref":
        return Ref(self.init, loc)


class Deref(Expr):
    """读取可变单元的当前内容；ref 必须求值为 RefVal。"""

    __slots__ = ("ref",)
    kind = "Deref"

    def __init__(self, ref: Expr, loc: Loc = None):
        super().__init__(loc)
        self.ref = _check_child(ref, "Deref.ref", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.ref,)

    def with_loc(self, loc: Loc) -> "Deref":
        return Deref(self.ref, loc)


class Assign(Expr):
    """把 value 写入 ref 单元；返回写入的 value。

    按值左到右：先求 ref 再求 value。
    """

    __slots__ = ("ref", "value")
    kind = "Assign"

    def __init__(self, ref: Expr, value: Expr, loc: Loc = None):
        super().__init__(loc)
        self.ref = _check_child(ref, "Assign.ref", loc)
        self.value = _check_child(value, "Assign.value", loc)

    def children(self) -> Tuple["Expr", ...]:
        return (self.ref, self.value)

    def with_loc(self, loc: Loc) -> "Assign":
        return Assign(self.ref, self.value, loc)


class Choose(Expr):
    """有限多选效果：候选必须是**显式有限**的 Expr 序列。

    仅在 :func:`delimcc.search` / :func:`delimcc.iter_search` 的搜索
    处理器内有效：搜索在分叉点快照当前机器状态（含 store），按候选
    顺序广度轮转。空候选与 :class:`Fail` 等价（结束当前分支）。
    原入口 :func:`delimcc.interpret` 不安装处理器，遇到它抛
    ``UnhandledEffectError``。
    """

    __slots__ = ("options",)
    kind = "Choose"

    def __init__(self, options, loc: Loc = None):
        super().__init__(loc)
        if not isinstance(options, (tuple, list)):
            raise InvalidProgramError(
                f"Choose.options 必须是 tuple/list（显式有限候选），得到 {type(options).__name__}",
                loc,
            )
        checked = tuple(
            _check_child(o, f"Choose.options[{i}]", loc)
            for i, o in enumerate(options)
        )
        self.options = checked

    def children(self) -> Tuple["Expr", ...]:
        return tuple(self.options)

    def with_loc(self, loc: Loc) -> "Choose":
        return Choose(self.options, loc)


class Fail(Expr):
    """失败效果：只结束当前搜索分支，不影响其他候选。

    未被搜索处理器捕获时（``interpret``）抛 ``UnhandledEffectError``。
    ``Fail`` 无子表达式、无参数。
    """

    __slots__ = ()
    kind = "Fail"

    def __init__(self, loc: Loc = None):
        super().__init__(loc)

    def children(self) -> Tuple["Expr", ...]:
        return ()

    def with_loc(self, loc: Loc) -> "Fail":
        return Fail(loc)


def _check_child(e: object, where: str, loc: Loc) -> Expr:
    if not isinstance(e, Expr):
        raise InvalidProgramError(
            f"{where} 必须是 Expr 节点，得到 {type(e).__name__}: {e!r}", loc
        )
    if e.kind not in _VALID:
        raise InvalidProgramError(f"{where} 的节点种类非法: {e.kind!r}", loc)
    return e


#: 求值结果中可能出现的值（AST 侧只含 int 标签）。
Tag = int

# 重新导出 Union 以便上层类型注解。
Node = Union[Int, Var, Add, Lam, Call, Prompt, Shift]
