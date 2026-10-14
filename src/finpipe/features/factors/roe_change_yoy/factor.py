"""披露ROE同比相对变化：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, ratio


def calculate(context: FactorContext) -> Value:
    """披露ROE同比相对变化；有效性、缺值和可比口径由共用运算保留。"""
    return ratio(
        context.read("roe_weighted_ytd", "indicator"),
        context.read("roe_weighted_ytd", "indicator", 4),
        context.ratio_floor,
        growth=True,
        check_basis=context.check_basis,
    )


FACTOR = FactorDefinition(
    name="roe_change_yoy",
    description="披露ROE同比相对变化",
    formula="ROE_ytd / lag4(ROE_ytd) - 1",
    dependencies=(("indicator", "roe_weighted_ytd", "ytd", (0, 4)),),
    unit="ratio",
    calculate=calculate,
    growth=True,
    warning_range=(-10, 10, "extreme_growth"),
)
