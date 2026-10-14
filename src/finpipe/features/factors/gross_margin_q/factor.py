"""单季毛利率：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, difference, ratio


def calculate(context: FactorContext) -> Value:
    """单季毛利率；有效性、缺值和可比口径由共用运算保留。"""
    revenue = context.quarter("operating_revenue_ytd")
    return ratio(
        difference(revenue, context.quarter("operating_cost_ytd")), revenue, context.amount_floor
    )


FACTOR = FactorDefinition(
    name="gross_margin_q",
    description="单季毛利率",
    formula="(R_q - C_q) / R_q",
    dependencies=(
        ("income", "operating_revenue_ytd", "quarter", (0,)),
        ("income", "operating_cost_ytd", "quarter", (0,)),
    ),
    unit="ratio",
    calculate=calculate,
    warning_range=(-1, 1, "extreme_gross_margin"),
)
