"""编排纯因子计算，冻结状态、精确输入与版本指纹；公式在各因子目录。"""

import math
from dataclasses import asdict

from finpipe.features.common import Evaluation, FactorContext, hard_error, lag, range_warning
from finpipe.features.registry import FACTORS
from finpipe.features.specifications import feature_dictionary as feature_dictionary
from finpipe.identity import stable_id
from finpipe.models import CleanRecord, FeatureResult, RecordState
from finpipe.timeutil import quarter_end

STATUS_PRIORITY = (
    "not_applicable",
    "version_conflict",
    "input_error",
    "missing_input",
    "missing_history",
    "nonpositive_baseline",
    "invalid_denominator",
    "warning",
    "ok",
)


def required_periods(output_periods: tuple[str, ...]) -> tuple[str, ...]:
    """从真实登记规格展开依赖；单季流量补充同年紧邻累计期。"""
    required = set()
    for target in output_periods:
        for factor in FACTORS.values():
            for _, _, basis, offsets in factor.dependencies:
                for offset in offsets:
                    period = lag(target, offset)
                    required.add(period)
                    if basis == "quarter" and period[5:] != "03-31":
                        required.add(lag(period, 1))
    return tuple(sorted(required))


def compute_features(
    records: list[CleanRecord],
    states: list[RecordState],
    codes: tuple[str, ...],
    output_periods: tuple[str, ...],
    data_as_of: str,
    profile_id: str,
    scope_id: str,
    rules: dict | None = None,
    *,
    formula_version: str = "2",
) -> list[FeatureResult]:
    """每个目标按登记规格保留完整网格含NULL；输入指纹不包含本次执行时刻或截止参数。"""
    if formula_version not in ("1", "2"):
        raise ValueError("未知特征公式版本")
    # 旧冻结运行保留公式1；新运行以公式2检查所有跨期比较的已知口径风险。
    check_basis = formula_version == "2"
    rules = rules or {}
    amount_floor, ratio_floor = (
        rules.get("amount_denominator", 1.0),
        rules.get("ratio_denominator", 1e-8),
    )
    for threshold in (amount_floor, ratio_floor):
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or threshold < 0
        ):
            raise ValueError("分母阈值必须为有限非负数")
    if len(set(codes)) != len(codes) or len(set(output_periods)) != len(output_periods):
        raise ValueError("目标公司和报告期不能重复")
    context = Evaluation(records, states, data_as_of)
    results = []
    for code in sorted(codes):
        for period in sorted(quarter_end(value) for value in output_periods):
            target = FactorContext(context, code, period, amount_floor, ratio_floor, check_basis)
            for factor in FACTORS.values():
                name = factor.name
                computed = factor.calculate(target)
                if factor.warning_range is not None:
                    computed = range_warning(computed, *factor.warning_range)
                status = next(
                    item for item in STATUS_PRIORITY if item in computed.statuses or item == "ok"
                )
                value = None if hard_error(computed) else computed.value
                if value is None and status in ("ok", "warning"):
                    raise ValueError("缺少数值的结果必须带明确不可用状态")
                unit = factor.unit
                input_hash = stable_id(
                    "input",
                    {
                        "scope_id": scope_id,
                        "profile_id": profile_id,
                        "inputs": [asdict(item) for item in computed.inputs],
                        "reasons": computed.reasons,
                        "flags": computed.flags,
                        "basis": computed.bases,
                        "denominator_thresholds": (amount_floor, ratio_floor),
                    },
                )
                feature_id = stable_id(
                    "feature",
                    {
                        "scope_id": scope_id,
                        "profile_id": profile_id,
                        "target": (code, period, name),
                        "input_hash": input_hash,
                    },
                )
                results.append(
                    FeatureResult(
                        feature_id,
                        code,
                        period,
                        name,
                        value,
                        status,
                        unit,
                        computed.reasons,
                        computed.flags,
                        max(item.input_available_at for item in computed.inputs),
                        input_hash,
                        computed.inputs,
                    )
                )
    return results
