"""精确季度关联、金融安全计算与不可变输入血缘。"""

import math
from dataclasses import asdict, dataclass

from finpipe.features.specifications import FEATURE_NAMES, _feature_unit
from finpipe.features.specifications import feature_dictionary as feature_dictionary
from finpipe.identity import stable_id
from finpipe.models import CleanRecord, FeatureInput, FeatureResult, RecordState
from finpipe.timeutil import iso_time, quarter_end

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


def _lag(period: str, count: int) -> str:
    """按自然年季度序号回退，Q1环比准确落在上一年Q4。"""
    period = quarter_end(period)
    quarter = (int(period[5:7]) // 3) - 1
    year, quarter = divmod(int(period[:4]) * 4 + quarter - count, 4)
    return f"{year:04d}-" + ("03-31", "06-30", "09-30", "12-31")[quarter]


def required_periods(output_periods: tuple[str, ...]) -> tuple[str, ...]:
    """展开本版公式原始累计依赖，供编排生成扫描或范围预检事实。"""
    required = set()
    for target in output_periods:
        for offset in (0, 1, 4):
            period = _lag(target, offset)
            required.add(period)
            if period[5:] != "03-31":
                required.add(_lag(period, 1))
    return tuple(sorted(required))


@dataclass(frozen=True)
class _Value:
    """中间数值与完整输入共同传递，错误不会被后续算术隐藏。"""

    value: float | None
    statuses: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    inputs: tuple[FeatureInput, ...] = ()
    bases: tuple[str, ...] = ()


def _combine(
    *values: _Value, value: float | None = None, status: str = "", reason: str = "", flag: str = ""
) -> _Value:
    """相同原始依赖去重；准确角色不同的同字段仍分别保留。"""
    unique_inputs = {}
    for item in values:
        for dependency in item.inputs:
            unique_inputs[(dependency.key, dependency.field_name, dependency.input_role)] = (
                dependency
            )
    inputs = tuple(
        sorted(
            unique_inputs.values(),
            key=lambda item: (
                item.key.security_code,
                item.key.report_end,
                item.key.statement_type,
                item.field_name,
                item.input_role,
            ),
        )
    )
    return _Value(
        value,
        tuple(
            sorted(
                set(item for source in values for item in source.statuses)
                | ({status} if status else set())
            )
        ),
        tuple(
            sorted(
                set(item for source in values for item in source.reasons)
                | ({reason} if reason else set())
            )
        ),
        tuple(
            sorted(
                set(item for source in values for item in source.flags)
                | ({flag} if flag else set())
            )
        ),
        inputs,
        tuple(sorted(set(item for source in values for item in source.bases))),
    )


def _hard(value: _Value) -> bool:
    return any(status not in ("ok", "warning") for status in value.statuses)


def _comparable(value: _Value) -> _Value:
    """明确不可比只阻止跨期比较，单期财务值仍可展示和做同期比率。"""
    if "incompatible" in value.bases:
        return _combine(value, status="input_error", reason="incompatible_basis")
    if "known_risk_unresolved" in value.bases:
        return _combine(value, status="input_error", reason="known_restatement_risk")
    return value


def _difference(current: _Value, previous: _Value, check_basis: bool = False) -> _Value:
    combined = _combine(current, previous)
    if check_basis:
        combined = _comparable(combined)
    if _hard(combined):
        return combined
    result = current.value - previous.value
    if not math.isfinite(result):
        return _combine(combined, status="input_error", reason="nonfinite_calculation")
    return _combine(combined, value=result)


def _ratio(
    numerator: _Value,
    denominator: _Value,
    threshold: float,
    growth: bool = False,
    check_basis: bool = False,
) -> _Value:
    """普通比率拒绝无效分母；成长只接受足够大的正基期。"""
    combined = _combine(numerator, denominator)
    if check_basis:
        combined = _comparable(combined)
    if _hard(combined):
        return combined
    if growth and denominator.value <= 0:
        if denominator.value == 0:
            diagnostic = "zero_baseline"
        elif numerator.value > 0:
            diagnostic = "turn_profit"
        else:
            diagnostic = "continuing_loss"
        return _combine(
            combined,
            status="nonpositive_baseline",
            reason="zero_baseline" if denominator.value == 0 else "negative_baseline",
            flag=diagnostic,
        )
    if denominator.value <= threshold:
        return _combine(
            combined,
            status="invalid_denominator",
            reason="small_positive_denominator"
            if denominator.value > 0
            else "nonpositive_denominator",
        )
    result = numerator.value / denominator.value - (1.0 if growth else 0.0)
    if not math.isfinite(result):
        return _combine(combined, status="input_error", reason="nonfinite_calculation")
    flags = "turn_loss" if growth and numerator.value < 0 else ""
    return _combine(combined, value=result, flag=flags)


def _range_warning(value: _Value, low: float, high: float, reason: str) -> _Value:
    if value.value is not None and not low <= value.value <= high:
        return _combine(value, value=value.value, status="warning", reason=reason)
    return value


class _Evaluation:
    """一次调用固定来源、处理版和输入截止，只索引传入的不可变事实。"""

    def __init__(self, records: list[CleanRecord], states: list[RecordState], data_as_of: str):
        self.cutoff = iso_time(data_as_of)
        sources = {item.key.source for item in states} | {item.key.source for item in records}
        if len(sources) != 1:
            raise ValueError("特征计算必须固定一个明确来源，且有真实裁决或扫描事实")
        self.records = {}
        for record in records:
            if (
                record.source_version_id in self.records
                and self.records[record.source_version_id].clean_version_id
                != record.clean_version_id
            ):
                raise ValueError("同一次计算不能混入不同清洗处理版本")
            self.records[record.source_version_id] = record
        self.states = {}
        for state in states:
            if len(state.decision_observed_at) == 10:
                raise ValueError("真实裁决时刻必须为带时区的精确时间，不能用日期补造")
            boundary = iso_time(state.decision_observed_at)
            if state.state_status == "selected":
                selected = self.records.get(state.selected_source_version_id)
                if selected:
                    boundary = max(boundary, iso_time(selected.data_available_at))
            if boundary > self.cutoff:
                continue
            logical_key = (state.key.security_code, state.key.report_end, state.key.statement_type)
            prior = self.states.get(logical_key)
            if prior and boundary == prior[0] and state.record_state_id != prior[1].record_state_id:
                raise ValueError("相同有效时刻有多个裁决，须由编排明确状态先后证据")
            if prior is None or boundary > prior[0]:
                self.states[logical_key] = (boundary, state)

    def read(self, code: str, period: str, table: str, field: str, role: str) -> _Value:
        """先选择最新裁决再校验字段，不能过滤坏状态后取旧好值。"""
        selected = self.states.get((code, period, table))
        if selected is None:
            raise ValueError(
                f"缺少真实裁决/扫描证据：{code}/{period}/{table}；不能以计算截止伪造缺失时间"
            )
        boundary, state = selected
        record = (
            self.records.get(state.selected_source_version_id)
            if state.state_status == "selected"
            else None
        )
        reasons, flags = [], []
        status, input_status, value = "ok", "satisfied", None
        if state.state_status == "conflict":
            status, input_status = "version_conflict", "conflict"
            reasons.append("source_version_conflict")
        elif state.state_status == "missing":
            status = "missing_input" if role == "current" else "missing_history"
            input_status = "missing"
            reasons.append("missing_report" if role == "current" else "missing_comparison_period")
        elif state.state_status != "selected" or record is None:
            status, input_status = "input_error", "invalid"
            reasons.append("missing_clean_version")
        elif record.key != state.key:
            raise ValueError("清洗结果与裁决业务键不一致")
        else:
            value = record.values.get(field)
            field_status = record.field_states.get(field, "invalid")
            reasons.extend(record.reasons.get(field, ()))
            flags.extend(record.evidence_flags)
            if record.quality_status == "quarantined" or not record.time_valid:
                status, input_status, value = "input_error", "invalid", None
                reasons.extend(record.reasons.get("__record__", ()))
                reasons.extend(record.reasons.get("__time__", ()))
            elif field_status == "not_applicable":
                status, input_status, value = "not_applicable", "not_applicable", None
            elif field_status == "invalid" or (
                value is not None and (isinstance(value, bool) or not math.isfinite(value))
            ):
                status, input_status, value = "input_error", "invalid", None
                if not reasons:
                    reasons.append("invalid_financial_field")
            elif field_status == "missing" or value is None:
                status, input_status, value = "missing_input", "missing", None
                if not reasons:
                    reasons.append("source_null")
            elif (
                field_status == "warning" or record.reasons.get("__time__") or record.evidence_flags
            ):
                status = "warning"
                reasons.extend(record.reasons.get("__time__", ()))
        dependency = FeatureInput(
            state.key,
            field,
            role,
            state.record_state_id,
            record.clean_version_id if record else None,
            input_status,
            boundary,
            record.source_available_at if record else None,
            value,
            tuple(sorted(set(reasons))),
        )
        return _Value(
            value,
            (status,),
            tuple(sorted(set(reasons))),
            tuple(sorted(set(flags))),
            (dependency,),
            (record.basis_status,) if record else (),
        )

    def quarter(self, code: str, period: str, field: str, role: str = "current") -> _Value:
        """Q1直接使用累计值，其余仅减同年紧邻累计期并校验可比性。"""
        current = self.read(code, period, "income", field, role)
        if period[5:] == "03-31":
            return current
        previous = self.read(code, _lag(period, 1), "income", field, f"{role}.previous_ytd")
        combined = _combine(current, previous)
        if any(basis in ("known_risk_unresolved", "incompatible") for basis in combined.bases):
            return _comparable(combined)
        result = _difference(current, previous)
        if "source_assumed" in combined.bases:
            result = _combine(
                result,
                value=result.value,
                status="warning",
                reason="restatement_basis_unverified",
                flag="source_assumed_quarter",
            )
        return result


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
    """每个目标保留十五格含NULL；输入指纹不包含本次执行时刻或截止参数。"""
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
    context = _Evaluation(records, states, data_as_of)
    results = []
    for code in sorted(codes):
        for period in sorted(quarter_end(value) for value in output_periods):
            previous, last_year = _lag(period, 1), _lag(period, 4)

            def read(field, table="income", at=period, role="current"):
                return context.read(code, at, table, field, role)

            def quarter(field, at=period, role="current"):
                return context.quarter(code, at, field, role)

            roe = read("roe_weighted_ytd", "indicator")
            prior_roe = read("roe_weighted_ytd", "indicator", last_year, "lag4")
            revenue, cost, net = (
                quarter(field)
                for field in ("operating_revenue_ytd", "operating_cost_ytd", "net_profit_ytd")
            )
            revenue_ytd, cost_ytd, net_ytd = (
                read(field)
                for field in ("operating_revenue_ytd", "operating_cost_ytd", "net_profit_ytd")
            )
            parent = quarter("parent_net_profit_ytd")
            debt = _ratio(
                read("total_liabilities", "balance"), read("total_assets", "balance"), amount_floor
            )
            prior_debt = _ratio(
                read("total_liabilities", "balance", previous, "lag1"),
                read("total_assets", "balance", previous, "lag1"),
                amount_floor,
            )
            current = _ratio(
                read("current_assets", "balance"),
                read("current_liabilities", "balance"),
                amount_floor,
            )
            prior_current = _ratio(
                read("current_assets", "balance", previous, "lag1"),
                read("current_liabilities", "balance", previous, "lag1"),
                amount_floor,
            )
            values = {
                "roe_weighted_ytd": roe,
                "roe_change_yoy": _ratio(roe, prior_roe, ratio_floor, True, check_basis),
                "roe_change_yoy_pp": _difference(roe, prior_roe, check_basis),
                "net_margin_q": _range_warning(
                    _ratio(net, revenue, amount_floor), -5, 2, "extreme_net_margin"
                ),
                "gross_margin_q": _range_warning(
                    _ratio(_difference(revenue, cost), revenue, amount_floor),
                    -1,
                    1,
                    "extreme_gross_margin",
                ),
                "net_margin_ytd": _range_warning(
                    _ratio(net_ytd, revenue_ytd, amount_floor), -5, 2, "extreme_net_margin"
                ),
                "gross_margin_ytd": _range_warning(
                    _ratio(_difference(revenue_ytd, cost_ytd), revenue_ytd, amount_floor),
                    -1,
                    1,
                    "extreme_gross_margin",
                ),
                "revenue_growth_yoy": _ratio(
                    revenue,
                    quarter("operating_revenue_ytd", last_year, "lag4"),
                    amount_floor,
                    True,
                    check_basis,
                ),
                "revenue_growth_qoq": _ratio(
                    revenue,
                    quarter("operating_revenue_ytd", previous, "lag1"),
                    amount_floor,
                    True,
                    check_basis,
                ),
                "parent_profit_growth_yoy": _ratio(
                    parent,
                    quarter("parent_net_profit_ytd", last_year, "lag4"),
                    amount_floor,
                    True,
                    check_basis,
                ),
                "parent_profit_growth_qoq": _ratio(
                    parent,
                    quarter("parent_net_profit_ytd", previous, "lag1"),
                    amount_floor,
                    True,
                    check_basis,
                ),
                "debt_asset_ratio": _range_warning(debt, -math.inf, 1, "high_leverage"),
                "debt_asset_change_qoq_pp": _difference(debt, prior_debt, check_basis),
                "current_ratio": _range_warning(current, -math.inf, 20, "extreme_current_ratio"),
                "current_ratio_change_qoq": _difference(current, prior_current, check_basis),
            }
            for name in FEATURE_NAMES:
                computed = values[name]
                if "growth" in name or name == "roe_change_yoy":
                    computed = _range_warning(computed, -10, 10, "extreme_growth")
                status = next(
                    item for item in STATUS_PRIORITY if item in computed.statuses or item == "ok"
                )
                value = None if _hard(computed) else computed.value
                if value is None and status in ("ok", "warning"):
                    raise ValueError("缺少数值的结果必须带明确不可用状态")
                unit = _feature_unit(name)
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
