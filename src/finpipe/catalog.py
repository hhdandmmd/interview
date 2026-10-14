"""字段规格的唯一维护入口，驱动映射、来源摘要及发布字典。"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FieldSpec:
    """声明来源名称、单位转换、期间及可空性；存在性不等于非空。"""

    name: str
    source_field: str
    unit: str
    period_basis: str
    multiplier: float = 1.0
    required: bool = True
    description: str = ""
    update_policy: str = "event"


REPORT_NAMES = {
    "income": "RPT_F10_FINANCE_GINCOME",
    "balance": "RPT_F10_FINANCE_GBALANCE",
    "indicator": "RPT_F10_FINANCE_MAINFINADATA",
}
FIELDS = {
    "income": (
        FieldSpec("operating_revenue_ytd", "OPERATE_INCOME", "cny", "ytd", description="营业收入"),
        FieldSpec(
            "total_revenue_ytd", "TOTAL_OPERATE_INCOME", "cny", "ytd", description="营业总收入"
        ),
        FieldSpec("operating_cost_ytd", "OPERATE_COST", "cny", "ytd", description="营业成本"),
        FieldSpec("net_profit_ytd", "NETPROFIT", "cny", "ytd", description="合并净利润"),
        FieldSpec(
            "parent_net_profit_ytd", "PARENT_NETPROFIT", "cny", "ytd", description="归母净利润"
        ),
    ),
    "balance": (
        FieldSpec("total_assets", "TOTAL_ASSETS", "cny", "point_in_time", description="总资产"),
        FieldSpec(
            "total_liabilities", "TOTAL_LIABILITIES", "cny", "point_in_time", description="总负债"
        ),
        FieldSpec("total_equity", "TOTAL_EQUITY", "cny", "point_in_time", description="权益合计"),
        FieldSpec(
            "parent_equity", "TOTAL_PARENT_EQUITY", "cny", "point_in_time", description="归母权益"
        ),
        FieldSpec(
            "current_assets", "TOTAL_CURRENT_ASSETS", "cny", "point_in_time", description="流动资产"
        ),
        FieldSpec(
            "current_liabilities",
            "TOTAL_CURRENT_LIAB",
            "cny",
            "point_in_time",
            description="流动负债",
        ),
    ),
    "indicator": (
        FieldSpec("roe_weighted_ytd", "ROEJQ", "ratio", "ytd", 0.01, description="累计加权ROE"),
        FieldSpec(
            "source_gross_margin_ytd", "XSMLL", "ratio", "ytd", 0.01, description="累计毛利率"
        ),
        FieldSpec("source_net_margin_ytd", "XSJLL", "ratio", "ytd", 0.01, description="累计净利率"),
        FieldSpec(
            "source_debt_asset_ratio", "ZCFZL", "ratio", "point_in_time", 0.01, description="负债率"
        ),
        FieldSpec(
            "source_current_ratio", "LD", "multiple", "point_in_time", description="流动比率"
        ),
    ),
}


def field_dictionary() -> dict:
    """从实际字段规格生成字典，避免展示层维护第二套单位。"""
    return {name: [asdict(spec) for spec in specs] for name, specs in FIELDS.items()}
