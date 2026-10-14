"""累计合并净利率：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, ratio


def calculate(context: FactorContext) -> Value:
    """累计合并净利率；有效性、缺值和可比口径由共用运算保留。"""
    return ratio(
        context.read("net_profit_ytd"), context.read("operating_revenue_ytd"), context.amount_floor
    )


FACTOR = FactorDefinition(
    name="net_margin_ytd",
    description="累计合并净利率",
    formula="NP_ytd / R_ytd",
    dependencies=(
        ("income", "net_profit_ytd", "ytd", (0,)),
        ("income", "operating_revenue_ytd", "ytd", (0,)),
    ),
    unit="ratio",
    calculate=calculate,
    warning_range=(-5, 2, "extreme_net_margin"),
)
