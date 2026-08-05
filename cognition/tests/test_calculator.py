"""calculator 工具：合法 / 非法 / 除零 / 不允许的名字。"""

from cognition.tools.calculator import calculator
from cognition.tools.registry import get_local_tools


def _calc(expr: str) -> str:
    # 结构化工具：invoke 接收入参 dict。
    return calculator.invoke({"expression": expr})


def test_valid_expression():
    assert _calc("2*(3+4)") == "14"


def test_valid_with_power_and_unary():
    assert _calc("-2 ** 3 + 10") == "2"  # -(2**3)+10 = 2


def test_integer_float_folding():
    # 10/2 = 5.0 → 折叠为 "5"
    assert _calc("10 / 2") == "5"


def test_invalid_syntax_returns_error_string():
    out = _calc("2 *")
    assert out.startswith("Error:")


def test_division_by_zero():
    out = _calc("1/0")
    assert "division by zero" in out
    assert out.startswith("Error:")


def test_disallowed_name():
    # 变量名/函数调用不在白名单 → 错误字符串（绝不 eval）。
    assert _calc("foo + 1").startswith("Error:")
    assert _calc("__import__('os')").startswith("Error:")


def test_registry_exposes_calculator():
    tools = get_local_tools()
    names = [t.name for t in tools]
    # M2：注册表新增 write_report（产物落 MinIO）；calculator 仍在。
    assert "calculator" in names
    assert "write_report" in names
