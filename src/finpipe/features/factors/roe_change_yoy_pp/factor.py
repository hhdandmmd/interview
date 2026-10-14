"""披露ROE同比百分点变化：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, difference


def calculate(context: FactorContext) -> Value:
    """披露ROE同比百分点变化；有效性、缺值和可比口径由共用运算保留。"""
    return difference(
        context.read("roe_weighted_ytd", "indicator"),
        context.read("roe_weighted_ytd", "indicator", 4),
        context.check_basis,
    )


FACTOR = FactorDefinition(
    name="roe_change_yoy_pp",
    description="披露ROE同比百分点变化",
    formula="ROE_ytd - lag4(ROE_ytd)",
    dependencies=(("indicator", "roe_weighted_ytd", "ytd", (0, 4)),),
    unit="ratio_delta",
    calculate=calculate,
)
