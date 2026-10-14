"""负债率环比百分点变化：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, difference, ratio


def calculate(context: FactorContext) -> Value:
    """负债率环比百分点变化；有效性、缺值和可比口径由共用运算保留。"""
    current = ratio(
        context.read("total_liabilities", "balance"),
        context.read("total_assets", "balance"),
        context.amount_floor,
    )
    previous = ratio(
        context.read("total_liabilities", "balance", 1),
        context.read("total_assets", "balance", 1),
        context.amount_floor,
    )
    return difference(current, previous, check_basis=context.check_basis)


FACTOR = FactorDefinition(
    name="debt_asset_change_qoq_pp",
    description="负债率环比百分点变化",
    formula="L / A - lag1(L / A)",
    dependencies=(
        ("balance", "total_liabilities", "point_in_time", (0, 1)),
        ("balance", "total_assets", "point_in_time", (0, 1)),
    ),
    unit="ratio_delta",
    calculate=calculate,
)
