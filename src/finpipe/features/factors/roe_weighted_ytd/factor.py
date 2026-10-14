"""披露累计加权ROE：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value


def calculate(context: FactorContext) -> Value:
    """披露累计加权ROE；有效性、缺值和可比口径由共用运算保留。"""
    return context.read("roe_weighted_ytd", "indicator")


FACTOR = FactorDefinition(
    name="roe_weighted_ytd",
    description="披露累计加权ROE",
    formula="ROE_ytd",
    dependencies=(("indicator", "roe_weighted_ytd", "ytd", (0,)),),
    unit="ratio",
    calculate=calculate,
)
