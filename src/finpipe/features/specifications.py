"""特征描述、单位与准确依赖规格；公式只用于核验与发布说明。"""

_FEATURE_DETAILS = {
    "roe_weighted_ytd": (
        "披露累计加权ROE",
        "ROE_ytd",
        (("indicator", "roe_weighted_ytd", "ytd", (0,)),),
    ),
    "roe_change_yoy": (
        "披露ROE同比相对变化",
        "ROE_ytd / lag4(ROE_ytd) - 1",
        (("indicator", "roe_weighted_ytd", "ytd", (0, 4)),),
    ),
    "roe_change_yoy_pp": (
        "披露ROE同比百分点变化",
        "ROE_ytd - lag4(ROE_ytd)",
        (("indicator", "roe_weighted_ytd", "ytd", (0, 4)),),
    ),
    "net_margin_q": (
        "单季合并净利率",
        "NP_q / R_q",
        (
            ("income", "net_profit_ytd", "quarter", (0,)),
            ("income", "operating_revenue_ytd", "quarter", (0,)),
        ),
    ),
    "gross_margin_q": (
        "单季毛利率",
        "(R_q - C_q) / R_q",
        (
            ("income", "operating_revenue_ytd", "quarter", (0,)),
            ("income", "operating_cost_ytd", "quarter", (0,)),
        ),
    ),
    "net_margin_ytd": (
        "累计合并净利率",
        "NP_ytd / R_ytd",
        (
            ("income", "net_profit_ytd", "ytd", (0,)),
            ("income", "operating_revenue_ytd", "ytd", (0,)),
        ),
    ),
    "gross_margin_ytd": (
        "累计毛利率",
        "(R_ytd - C_ytd) / R_ytd",
        (
            ("income", "operating_revenue_ytd", "ytd", (0,)),
            ("income", "operating_cost_ytd", "ytd", (0,)),
        ),
    ),
    "revenue_growth_yoy": (
        "单季营业收入同比",
        "R_q / lag4(R_q) - 1",
        (("income", "operating_revenue_ytd", "quarter", (0, 4)),),
    ),
    "revenue_growth_qoq": (
        "单季营业收入环比",
        "R_q / lag1(R_q) - 1",
        (("income", "operating_revenue_ytd", "quarter", (0, 1)),),
    ),
    "parent_profit_growth_yoy": (
        "单季归母利润同比",
        "PNP_q / lag4(PNP_q) - 1",
        (("income", "parent_net_profit_ytd", "quarter", (0, 4)),),
    ),
    "parent_profit_growth_qoq": (
        "单季归母利润环比",
        "PNP_q / lag1(PNP_q) - 1",
        (("income", "parent_net_profit_ytd", "quarter", (0, 1)),),
    ),
    "debt_asset_ratio": (
        "期末资产负债率",
        "L / A",
        (
            ("balance", "total_liabilities", "point_in_time", (0,)),
            ("balance", "total_assets", "point_in_time", (0,)),
        ),
    ),
    "debt_asset_change_qoq_pp": (
        "负债率环比百分点变化",
        "L / A - lag1(L / A)",
        (
            ("balance", "total_liabilities", "point_in_time", (0, 1)),
            ("balance", "total_assets", "point_in_time", (0, 1)),
        ),
    ),
    "current_ratio": (
        "期末流动比率",
        "CA / CL",
        (
            ("balance", "current_assets", "point_in_time", (0,)),
            ("balance", "current_liabilities", "point_in_time", (0,)),
        ),
    ),
    "current_ratio_change_qoq": (
        "流动比率环比倍数变化",
        "CA / CL - lag1(CA / CL)",
        (
            ("balance", "current_assets", "point_in_time", (0, 1)),
            ("balance", "current_liabilities", "point_in_time", (0, 1)),
        ),
    ),
}
FEATURE_NAMES = tuple(_FEATURE_DETAILS)


def _feature_unit(name: str) -> str:
    if name.endswith("_pp"):
        return "ratio_delta"
    if name == "current_ratio_change_qoq":
        return "multiple_delta"
    if name == "current_ratio":
        return "multiple"
    return "ratio"


def feature_dictionary(formula_version: str = "2") -> list[dict]:
    """从本版特征规格生成机器字典；准确期依赖及NULL规则随输出冻结。"""
    entries = []
    for name in FEATURE_NAMES:
        description, formula, dependencies = _FEATURE_DETAILS[name]
        unit = _feature_unit(name)
        growth = "growth" in name or name == "roe_change_yoy"
        null_policy = "先查最新裁决及字段有效性；冲突、硬错误与准确依赖缺失输出NULL，保留所有原因。"
        if growth:
            null_policy += "成长基期须正且超过声明阈值；非正基期为nonpositive_baseline，过小正基期为invalid_denominator。"
        elif "/" in formula:
            null_policy += "金额分母须超过声明阈值，否则为invalid_denominator。"
        period_dependencies = []
        for table, field, basis, lags in dependencies:
            expansion = (
                "Q1仅本季累计；Q2至Q4减同年紧邻前季累计，准确lag1/lag4后分别展开。"
                if basis == "quarter"
                else "读取准确目标期及声明lag期；累计ROE与存量不做季度差分。"
            )
            period_dependencies.append(
                {
                    "statement_type": table,
                    "field_name": field,
                    "period_basis": basis,
                    "lags": list(lags),
                    "expansion": expansion,
                    "currency": "CNY",
                    "consolidation_scope": "consolidated",
                }
            )
        if any(item[2] == "quarter" for item in dependencies):
            null_policy += (
                "已知未解决重述或不可比基础为input_error；普通source_assumed差分保留值并warning。"
            )
        if formula_version == "2" and (
            growth or name.endswith("_pp") or name == "current_ratio_change_qoq"
        ):
            null_policy += (
                "跨期比较同时检查本期及基期，包括Q1；已知不可比或重述风险输出input_error。"
            )
        entries.append(
            {
                "name": name,
                "description": description,
                "formula": formula,
                "storage_unit": unit,
                "display_unit": "percentage_point"
                if unit == "ratio_delta"
                else "percent"
                if unit == "ratio"
                else "倍差"
                if unit == "multiple_delta"
                else "倍",
                "display_multiplier": 100 if unit in ("ratio", "ratio_delta") else 1,
                "period_dependencies": period_dependencies,
                "null_policy": null_policy,
                "formula_version": formula_version,
            }
        )
    return entries
