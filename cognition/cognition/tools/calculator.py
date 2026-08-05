"""calculator 本地工具：受限 AST 求值的四则/幂运算器。

安全要点：**绝不使用 eval()**。只用 ast.parse(mode="eval") 解析后在白名单节点上递归求值：
允许 +、-、*、/、**、括号、一元正负、数字字面量；其余一切（变量名、函数调用、属性
访问、下标、布尔/位运算等）一律拒绝。无效表达式/非法名/除零都返回 "Error: ..." 字符串，
不抛异常给上层（ToolNode 据此产出 status=success 的 ToolMessage）。
"""

from __future__ import annotations

import ast
import operator
from typing import Callable

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class CalcArgs(BaseModel):
    """calculator 的入参 schema。"""

    expression: str = Field(
        description="一个算术表达式，仅含数字、+ - * / **、括号与一元正负，例如 '2*(3+4)'。"
    )


# 二元运算白名单。
_BIN_OPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}

# 一元运算白名单。
_UNARY_OPS: dict[type, Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# 幂运算上限，避免 10**10**10 之类的资源耗尽。
_MAX_POW_EXPONENT = 1000


def _eval(node: ast.AST) -> float:
    """在白名单节点上递归求值；遇到任何非白名单节点抛 ValueError。"""
    if isinstance(node, ast.Expression):
        return _eval(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):  # bool 是 int 的子类，单独拒绝
            raise ValueError("disallowed literal: bool")
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"disallowed literal: {type(node.value).__name__}")

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"disallowed operator: {type(node.op).__name__}")
        left, right = _eval(node.left), _eval(node.right)
        if op is operator.pow and isinstance(right, (int, float)) and right > _MAX_POW_EXPONENT:
            raise ValueError("exponent too large")
        return op(left, right)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"disallowed unary operator: {type(node.op).__name__}")
        return op(_eval(node.operand))

    # 变量名、函数调用、属性访问、下标、比较、布尔/位运算等：一律拒绝。
    raise ValueError(f"disallowed expression: {type(node).__name__}")


def _calculate(expression: str) -> str:
    """求值并格式化为字符串（整数浮点折叠为整数）。可能抛异常，由 @tool 包装层兜底。"""
    tree = ast.parse(expression, mode="eval")
    result = _eval(tree)
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return str(result)


@tool("calculator", args_schema=CalcArgs)
def calculator(expression: str) -> str:
    """计算一个基础算术表达式（+ - * / ** 与括号、一元正负），返回结果字符串。

    适用于用户需要精确数值计算时。无效或不允许的表达式会返回以 "Error:" 开头的说明。
    """
    try:
        return _calculate(expression)
    except ZeroDivisionError:
        return "Error: division by zero"
    except (ValueError, SyntaxError, TypeError, OverflowError) as exc:
        return f"Error: invalid expression: {exc}"
