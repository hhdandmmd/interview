"""流动比率环比倍数变化：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, difference, ratio


def calculate(context: FactorContext) -> Value:
    """流动比率环比倍数变化；有效性、缺值和可比口径由共用运算保留。"""
    current = ratio(
        context.read("current_assets", "balance"),
        context.read("current_liabilities", "balance"),
        context.amount_floor,
    )
    previous = ratio(
        context.read("current_assets", "balance", 1),
        context.read("current_liabilities", "balance", 1),
        context.amount_floor,
    )
    return difference(current, previous, check_basis=context.check_basis)


FACTOR = FactorDefinition(
    name="current_ratio_change_qoq",
    description="流动比率环比倍数变化",
    formula="CA / CL - lag1(CA / CL)",
    dependencies=(
        ("balance", "current_assets", "point_in_time", (0, 1)),
        ("balance", "current_liabilities", "point_in_time", (0, 1)),
    ),
    unit="multiple_delta",
    calculate=calculate,
)
