"""期末流动比率：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, ratio


def calculate(context: FactorContext) -> Value:
    """期末流动比率；有效性、缺值和可比口径由共用运算保留。"""
    return ratio(
        context.read("current_assets", "balance"),
        context.read("current_liabilities", "balance"),
        context.amount_floor,
    )


FACTOR = FactorDefinition(
    name="current_ratio",
    description="期末流动比率",
    formula="CA / CL",
    dependencies=(
        ("balance", "current_assets", "point_in_time", (0,)),
        ("balance", "current_liabilities", "point_in_time", (0,)),
    ),
    unit="multiple",
    calculate=calculate,
    warning_range=(float("-inf"), 20, "extreme_current_ratio"),
)
