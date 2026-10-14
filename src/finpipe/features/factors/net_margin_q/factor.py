"""单季合并净利率：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, ratio


def calculate(context: FactorContext) -> Value:
    """单季合并净利率；有效性、缺值和可比口径由共用运算保留。"""
    return ratio(
        context.quarter("net_profit_ytd"),
        context.quarter("operating_revenue_ytd"),
        context.amount_floor,
    )


FACTOR = FactorDefinition(
    name="net_margin_q",
    description="单季合并净利率",
    formula="NP_q / R_q",
    dependencies=(
        ("income", "net_profit_ytd", "quarter", (0,)),
        ("income", "operating_revenue_ytd", "quarter", (0,)),
    ),
    unit="ratio",
    calculate=calculate,
    warning_range=(-5, 2, "extreme_net_margin"),
)
