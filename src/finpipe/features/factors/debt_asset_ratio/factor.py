"""期末资产负债率：公式、依赖、单位与计算在本目录统一维护。"""

from finpipe.features.common import FactorContext, FactorDefinition, Value, ratio


def calculate(context: FactorContext) -> Value:
    """期末资产负债率；有效性、缺值和可比口径由共用运算保留。"""
    return ratio(
        context.read("total_liabilities", "balance"),
        context.read("total_assets", "balance"),
        context.amount_floor,
    )


FACTOR = FactorDefinition(
    name="debt_asset_ratio",
    description="期末资产负债率",
    formula="L / A",
    dependencies=(
        ("balance", "total_liabilities", "point_in_time", (0,)),
        ("balance", "total_assets", "point_in_time", (0,)),
    ),
    unit="ratio",
    calculate=calculate,
    warning_range=(float("-inf"), 1, "high_leverage"),
)
