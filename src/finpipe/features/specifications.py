"""由独立因子定义生成字典，避免公式和依赖维护两套。"""

from finpipe.features.registry import FACTORS, FEATURE_NAMES


def _feature_unit(name: str) -> str:
    """按明确规格读取单位，不从因子名称猜测。"""
    return FACTORS[name].unit


def feature_dictionary(formula_version: str = "2") -> list[dict]:
    """从本版特征规格生成机器字典；准确期依赖及NULL规则随输出冻结。"""
    entries = []
    for name in FEATURE_NAMES:
        factor = FACTORS[name]
        description, formula, dependencies = factor.description, factor.formula, factor.dependencies
        unit = _feature_unit(name)
        growth = factor.growth
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
            any(offset > 0 for dependency in dependencies for offset in dependency[3])
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
