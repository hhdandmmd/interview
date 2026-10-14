"""单季归母利润同比：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, ratio


def calculate(context: FactorContext) -> Value:
    """单季归母利润同比；有效性、缺值和可比口径由共用运算保留。"""
    return ratio(
        context.quarter("parent_net_profit_ytd"),
        context.quarter("parent_net_profit_ytd", 4),
        context.amount_floor,
        growth=True,
        check_basis=context.check_basis,
    )


FACTOR = FactorDefinition(
    name="parent_profit_growth_yoy",
    description="单季归母利润同比",
    formula="PNP_q / lag4(PNP_q) - 1",
    dependencies=(("income", "parent_net_profit_ytd", "quarter", (0, 4)),),
    unit="ratio",
    calculate=calculate,
    growth=True,
    warning_range=(-10, 10, "extreme_growth"),
)
