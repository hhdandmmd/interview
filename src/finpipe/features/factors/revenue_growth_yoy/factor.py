"""单季营业收入同比：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, ratio


def calculate(context: FactorContext) -> Value:
    """单季营业收入同比；有效性、缺值和可比口径由共用运算保留。"""
    return ratio(
        context.quarter("operating_revenue_ytd"),
        context.quarter("operating_revenue_ytd", 4),
        context.amount_floor,
        growth=True,
        check_basis=context.check_basis,
    )


FACTOR = FactorDefinition(
    name="revenue_growth_yoy",
    description="单季营业收入同比",
    formula="R_q / lag4(R_q) - 1",
    dependencies=(("income", "operating_revenue_ytd", "quarter", (0, 4)),),
    unit="ratio",
    calculate=calculate,
    growth=True,
    warning_range=(-10, 10, "extreme_growth"),
)
