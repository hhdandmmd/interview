"""因子共用的准确季度、数值状态和输入血缘；全部计算为纯函数。"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from finpipe.models import CleanRecord, FeatureInput, RecordState
from finpipe.timeutil import iso_time, quarter_end


def lag(period: str, count: int) -> str:
    """按自然年季度序号回退，Q1环比准确落在上一年Q4。"""
    period = quarter_end(period)
    quarter = (int(period[5:7]) // 3) - 1
    year, quarter = divmod(int(period[:4]) * 4 + quarter - count, 4)
    return f"{year:04d}-" + ("03-31", "06-30", "09-30", "12-31")[quarter]


@dataclass(frozen=True)
class Value:
    """中间数值与完整输入共同传递，错误不会被后续算术隐藏。"""

    value: float | None
    statuses: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    inputs: tuple[FeatureInput, ...] = ()
    bases: tuple[str, ...] = ()


def combine(
    *values: Value, value: float | None = None, status: str = "", reason: str = "", flag: str = ""
) -> Value:
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
    return Value(
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


def hard_error(value: Value) -> bool:
    return any(status not in ("ok", "warning") for status in value.statuses)


def comparable(value: Value) -> Value:
    """明确不可比只阻止跨期比较，单期财务值仍可展示和做同期比率。"""
    if "incompatible" in value.bases:
        return combine(value, status="input_error", reason="incompatible_basis")
    if "known_risk_unresolved" in value.bases:
        return combine(value, status="input_error", reason="known_restatement_risk")
    return value


def difference(current: Value, previous: Value, check_basis: bool = False) -> Value:
    combined = combine(current, previous)
    if check_basis:
        combined = comparable(combined)
    if hard_error(combined):
        return combined
    result = current.value - previous.value
    if not math.isfinite(result):
        return combine(combined, status="input_error", reason="nonfinite_calculation")
    return combine(combined, value=result)


def ratio(
    numerator: Value,
    denominator: Value,
    threshold: float,
    growth: bool = False,
    check_basis: bool = False,
) -> Value:
    """普通比率拒绝无效分母；成长只接受足够大的正基期。"""
    combined = combine(numerator, denominator)
    if check_basis:
        combined = comparable(combined)
    if hard_error(combined):
        return combined
    if growth and denominator.value <= 0:
        if denominator.value == 0:
            diagnostic = "zero_baseline"
        elif numerator.value > 0:
            diagnostic = "turn_profit"
        else:
            diagnostic = "continuing_loss"
        return combine(
            combined,
            status="nonpositive_baseline",
            reason="zero_baseline" if denominator.value == 0 else "negative_baseline",
            flag=diagnostic,
        )
    if denominator.value <= threshold:
        return combine(
            combined,
            status="invalid_denominator",
            reason="small_positive_denominator"
            if denominator.value > 0
            else "nonpositive_denominator",
        )
    result = numerator.value / denominator.value - (1.0 if growth else 0.0)
    if not math.isfinite(result):
        return combine(combined, status="input_error", reason="nonfinite_calculation")
    flags = "turn_loss" if growth and numerator.value < 0 else ""
    return combine(combined, value=result, flag=flags)


def range_warning(value: Value, low: float, high: float, reason: str) -> Value:
    if value.value is not None and not low <= value.value <= high:
        return combine(value, value=value.value, status="warning", reason=reason)
    return value


class Evaluation:
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

    def read(self, code: str, period: str, table: str, field: str, role: str) -> Value:
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
        return Value(
            value,
            (status,),
            tuple(sorted(set(reasons))),
            tuple(sorted(set(flags))),
            (dependency,),
            (record.basis_status,) if record else (),
        )

    def quarter(self, code: str, period: str, field: str, role: str = "current") -> Value:
        """Q1直接使用累计值，其余仅减同年紧邻累计期并校验可比性。"""
        current = self.read(code, period, "income", field, role)
        if period[5:] == "03-31":
            return current
        previous = self.read(code, lag(period, 1), "income", field, f"{role}.previous_ytd")
        combined = combine(current, previous)
        if any(basis in ("known_risk_unresolved", "incompatible") for basis in combined.bases):
            return comparable(combined)
        result = difference(current, previous)
        if "source_assumed" in combined.bases:
            result = combine(
                result,
                value=result.value,
                status="warning",
                reason="restatement_basis_unverified",
                flag="source_assumed_quarter",
            )
        return result


@dataclass(frozen=True)
class FactorContext:
    """固定一个公司报告期与计算口径；读取准确滞后，完整保留输入状态。"""

    evaluation: Evaluation
    code: str
    period: str
    amount_floor: float
    ratio_floor: float
    check_basis: bool
    # 只在一个公司报告期内复用不可变读取，不跨运行缓存来源或状态。
    _values: dict = field(default_factory=dict, repr=False, compare=False)

    def read(self, field: str, table: str = "income", offset: int = 0) -> Value:
        """读取累计或期末字段；角色与既有版本保持一致。"""
        key = ("read", table, field, offset)
        if key not in self._values:
            role = "current" if offset == 0 else f"lag{offset}"
            self._values[key] = self.evaluation.read(
                self.code, lag(self.period, offset), table, field, role
            )
        return self._values[key]

    def quarter(self, field: str, offset: int = 0) -> Value:
        """读取单季流量；比较期也展开准确的同年累计差分。"""
        key = ("quarter", "income", field, offset)
        if key not in self._values:
            role = "current" if offset == 0 else f"lag{offset}"
            self._values[key] = self.evaluation.quarter(
                self.code, lag(self.period, offset), field, role
            )
        return self._values[key]


@dataclass(frozen=True)
class FactorDefinition:
    """一个因子的规格与计算函数同处维护；公式文本用于说明，不执行表达式。"""

    name: str
    description: str
    formula: str
    dependencies: tuple[tuple[str, str, str, tuple[int, ...]], ...]
    unit: str
    calculate: Callable[[FactorContext], Value]
    growth: bool = False
    warning_range: tuple[float, float, str] | None = None
