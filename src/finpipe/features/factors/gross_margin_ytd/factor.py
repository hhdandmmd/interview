"""累计毛利率：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, difference, ratio


def calculate(context: FactorContext) -> Value:
    """累计毛利率；有效性、缺值和可比口径由共用运算保留。"""
    revenue = context.read("operating_revenue_ytd")
    return ratio(
        difference(revenue, context.read("operating_cost_ytd")), revenue, context.amount_floor
    )


FACTOR = FactorDefinition(
    name="gross_margin_ytd",
    description="累计毛利率",
    formula="(R_ytd - C_ytd) / R_ytd",
    dependencies=(
        ("income", "operating_revenue_ytd", "ytd", (0,)),
        ("income", "operating_cost_ytd", "ytd", (0,)),
    ),
    unit="ratio",
    calculate=calculate,
    warning_range=(-1, 1, "extreme_gross_margin"),
)
