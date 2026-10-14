"""冻结集合上的质量门禁与同源核对；纯函数不另建数据权威。"""

import math
from collections import Counter
from dataclasses import asdict
from statistics import median

from finpipe.catalog import FIELDS
from finpipe.identity import stable_id
from finpipe.models import BusinessKey, CleanRecord, FeatureResult, RecordState

# 解释字段仍须出现在来源契约中，其NULL不自动否定核心财务表可用性。
USABLE_FIELDS = {
    "income": (
        "operating_revenue_ytd",
        "operating_cost_ytd",
        "net_profit_ytd",
        "parent_net_profit_ytd",
    ),
    "balance": ("total_assets", "total_liabilities", "current_assets", "current_liabilities"),
    "indicator": ("roe_weighted_ytd",),
}
FACTOR_IMPACTS = {
    "income": (
        "net_margin_q",
        "gross_margin_q",
        "net_margin_ytd",
        "gross_margin_ytd",
        "revenue_growth_yoy",
        "revenue_growth_qoq",
        "parent_profit_growth_yoy",
        "parent_profit_growth_qoq",
    ),
    "balance": (
        "debt_asset_ratio",
        "debt_asset_change_qoq_pp",
        "current_ratio",
        "current_ratio_change_qoq",
    ),
    "indicator": ("roe_weighted_ytd", "roe_change_yoy", "roe_change_yoy_pp"),
}


def _rate(numerator: int, denominator: int) -> float | None:
    """零分母保留NULL，不展示虚构的百分之百。"""
    return numerator / denominator if denominator else None


def _event(
    rule: str,
    severity: str,
    message: str,
    key: BusinessKey | None = None,
    record: CleanRecord | None = None,
    fields: tuple[str, ...] = (),
    evidence: dict | None = None,
) -> dict:
    details = {
        "rule_id": rule,
        "severity": severity,
        "message": message,
        "key": asdict(key) if key else None,
        "fields": list(fields),
        "source_version_id": record.source_version_id if record else None,
        "clean_version_id": record.clean_version_id if record else None,
        "affected_factors": list(FACTOR_IMPACTS.get(key.statement_type, ())) if key else [],
        "evidence": evidence or {},
        "gate_action": "notify",
    }
    return {"event_id": stable_id("quality", details), **details}


def _valid_record(record: CleanRecord | None) -> bool:
    return bool(record and record.time_valid and record.quality_status != "quarantined")


def _field(record: CleanRecord | None, name: str) -> tuple[float | None, str]:
    if not record:
        return None, "missing"
    if not _valid_record(record):
        return None, "invalid"
    state = record.field_states.get(name, "invalid")
    value = record.values.get(name)
    if state in ("invalid", "not_applicable"):
        return None, state
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, "invalid"
    return value, state


def _reference(
    record: CleanRecord | None, key: BusinessKey, field: str, state: RecordState | None
) -> dict:
    value, status = _field(record, field)
    return {
        "key": asdict(key),
        "field_name": field,
        "value": value,
        "field_status": status,
        "clean_version_id": record.clean_version_id if record else None,
        "source_version_id": record.source_version_id if record else None,
        "record_state_id": state.record_state_id if state else None,
        "state_status": state.state_status if state else None,
        "data_available_at": max(record.data_available_at, state.decision_observed_at)
        if record and state
        else record.data_available_at
        if record
        else state.decision_observed_at
        if state
        else None,
        "source_available_at": record.source_available_at if record else None,
    }


def _reconcile(
    selected: dict[BusinessKey, CleanRecord],
    planned: list[dict],
    floor: float,
    states: dict[BusinessKey, RecordState],
) -> list[dict]:
    """分母及来源规格相同才核对数值；同源吻合从不标记独立验证。"""
    groups = sorted(
        {
            (
                item["key"].source,
                item["key"].security_code,
                item["key"].report_end,
                item["key"].consolidation_scope,
            )
            for item in planned
        }
    )
    checks = (
        (
            "gross_margin_ytd",
            "income",
            "operating_revenue_ytd",
            "operating_cost_ytd",
            "source_gross_margin_ytd",
            5e-5,
        ),
        (
            "net_margin_ytd",
            "income",
            "net_profit_ytd",
            "operating_revenue_ytd",
            "source_net_margin_ytd",
            5e-5,
        ),
        (
            "debt_asset_ratio",
            "balance",
            "total_liabilities",
            "total_assets",
            "source_debt_asset_ratio",
            5e-5,
        ),
        (
            "current_ratio",
            "balance",
            "current_assets",
            "current_liabilities",
            "source_current_ratio",
            1e-6,
        ),
    )
    output = []
    for source, code, period, scope in groups:
        indicator_key = BusinessKey(source, code, period, "indicator", scope)
        indicator = selected.get(indicator_key)
        for name, table, numerator_name, denominator_name, source_field, tolerance in checks:
            key = BusinessKey(source, code, period, table, scope)
            record = selected.get(key)
            numerator, numerator_status = _field(record, numerator_name)
            denominator, denominator_status = _field(record, denominator_name)
            right, right_status = _field(indicator, source_field)
            left, reasons, result = None, [], "not_comparable"
            if name == "gross_margin_ytd":
                # 第一项为收入、第二项为成本；毛利分母必须使用营业收入。
                revenue, cost = numerator, denominator
                if revenue is not None and cost is not None and revenue > floor:
                    left = (revenue - cost) / revenue
            elif numerator is not None and denominator is not None and denominator > floor:
                left = numerator / denominator
            if left is not None and not math.isfinite(left):
                left = None
                reasons.append("nonfinite_calculation")
            unsafe = "invalid" in (
                numerator_status,
                denominator_status,
                right_status,
            ) or "not_applicable" in (numerator_status, denominator_status, right_status)
            if unsafe:
                reasons.append("invalid_or_inapplicable_comparison_input")
            elif numerator is not None and denominator is not None and left is None:
                reasons.append("invalid_denominator")
            elif name == "net_margin_ytd" and record:
                total, total_status = _field(record, "total_revenue_ytd")
                if total is None or total_status in ("invalid", "not_applicable"):
                    reasons.append("unverified_net_margin_denominator")
                elif denominator is not None and abs(total - denominator) > max(
                    1.0, abs(denominator) * 1e-6
                ):
                    reasons.append("unverified_net_margin_denominator")
            if any(
                states.get(candidate) and states[candidate].state_status == "conflict"
                for candidate in (key, indicator_key)
            ):
                reasons.append("source_version_conflict")
            if not reasons:
                if left is None and right is None:
                    result = "both_missing"
                elif left is None:
                    result = "missing_left"
                elif right is None:
                    result = "missing_right"
                else:
                    result = (
                        "matched"
                        if abs(left - right) <= tolerance + abs(right) * 1e-6
                        else "mismatched"
                    )
            detail = {
                "check_name": name,
                "security_code": code,
                "report_end": period,
                "result": result,
                "left_value": left,
                "right_value": right,
                "absolute_difference": abs(left - right)
                if left is not None and right is not None
                else None,
                "absolute_tolerance": tolerance,
                "relative_tolerance": 1e-6,
                "evidence_tier": "same_origin_check",
                "reason_codes": reasons,
                "left_inputs": [
                    _reference(record, key, numerator_name, states.get(key)),
                    _reference(record, key, denominator_name, states.get(key)),
                ],
                "right_inputs": [
                    _reference(indicator, indicator_key, source_field, states.get(indicator_key))
                ],
            }
            if name == "net_margin_ytd":
                detail["left_inputs"].append(
                    _reference(record, key, "total_revenue_ytd", states.get(key))
                )
            output.append({"reconciliation_id": stable_id("reconciliation", detail), **detail})
    return output


def _basis_coverage(features: list[FeatureResult], records: list[CleanRecord]) -> dict:
    """按准确季度/字段输入对统计独立可比覆盖，普通源假设不计核验。"""
    by_clean = {item.clean_version_id: item for item in records}
    expected, verified = set(), set()
    for feature in features:
        for dependency in feature.inputs:
            if not dependency.input_role.endswith(".previous_ytd"):
                continue
            role = dependency.input_role.removesuffix(".previous_ytd")
            current = next(
                (
                    item
                    for item in feature.inputs
                    if item.input_role == role and item.field_name == dependency.field_name
                ),
                None,
            )
            if current is None:
                continue
            pair = (current.key.security_code, current.key.report_end, current.field_name)
            expected.add(pair)
            if all(
                by_clean.get(item.clean_version_id)
                and by_clean[item.clean_version_id].basis_status == "verified_comparable"
                for item in (current, dependency)
            ):
                verified.add(pair)
    return {
        "quarter_derivation_key_count": len(expected),
        "verified_basis_key_count": len(verified),
        "verified_basis_coverage_rate": _rate(len(verified), len(expected)),
    }


def _baseline(metrics: dict, previous: dict | None) -> dict:
    """成功且同范围同口径的基线由调用方固定；不足或MAD为零单独解释。"""
    if not previous:
        return {
            "baseline_status": "baseline_insufficient",
            "baseline_sample_count": 0,
            "row_count_change": None,
            "field_null_rate_change": None,
            "row_count_mad": None,
        }
    prior = previous.get("metrics", previous)
    old_count = prior.get("received_count")
    old_null = prior.get("field_null_rate")
    result = {
        "baseline_status": "comparable_previous",
        "baseline_sample_count": 1,
        "row_count_change": (metrics["received_count"] / old_count - 1)
        if old_count and old_count > 0
        else None,
        "field_null_rate_change": metrics["field_null_rate"] - old_null
        if metrics["field_null_rate"] is not None and old_null is not None
        else None,
        "row_count_mad": None,
    }
    samples = previous.get("successful_row_counts", [])
    samples = [
        float(value)
        for value in samples
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    ]
    if samples:
        result["baseline_sample_count"] = len(samples)
        if len(samples) < 5:
            result["baseline_status"] = "baseline_insufficient"
        else:
            center = median(samples)
            mad = median(abs(value - center) for value in samples)
            result.update(
                row_count_median=center,
                row_count_mad=mad,
                baseline_status="mad_zero" if mad == 0 else "ready",
                robust_row_score=abs(metrics["received_count"] - center) / (1.4826 * mad)
                if mad
                else None,
            )
    return result


def evaluate_quality(
    records: list[CleanRecord],
    states: list[RecordState],
    features: list[FeatureResult],
    profile: dict,
    expected_keys: list[dict],
    previous: dict | None = None,
) -> dict:
    """门禁按保守分母计算；局部坏值以NULL及事件表达，再执行范围预算。"""
    events, blocking = [], []
    quality = profile.get("quality", {})
    minimum = quality.get("min_usable_coverage", 0.95)
    maximum_invalid = quality.get("max_invalid_rate", 0.01)
    for value in (minimum, maximum_invalid):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("质量门禁阈值必须在零至一之间")
    by_source = {}
    for record in records:
        prior = by_source.get(record.source_version_id)
        if prior and prior.clean_version_id != record.clean_version_id:
            raise ValueError("质量检查不能混合清洗处理版本")
        by_source[record.source_version_id] = record
    latest = {}
    for state in states:
        if (
            state.key not in latest
            or state.decision_observed_at > latest[state.key].decision_observed_at
        ):
            latest[state.key] = state
    selected = {}
    for key, state in latest.items():
        record = (
            by_source.get(state.selected_source_version_id)
            if state.state_status == "selected"
            else None
        )
        if record:
            if record.key != key:
                raise ValueError("质量检查收到不一致的来源与裁决业务键")
            selected[key] = record
        if state.state_status == "conflict":
            events.append(
                _event(
                    "source_version_conflict",
                    "error",
                    "财报来源版本冲突，相关因子输出NULL",
                    key,
                    evidence={
                        "record_state_id": state.record_state_id,
                        "decision_observed_at": state.decision_observed_at,
                    },
                )
            )
    planned, excluded, seen = [], [], set()
    for item in expected_keys:
        key = item["key"]
        if key in seen:
            blocking.append("duplicate_expected_key")
            continue
        seen.add(key)
        eligibility = item.get("eligibility", "unknown")
        if eligibility not in ("eligible", "unknown", "not_required"):
            raise ValueError("未知资格状态")
        if eligibility == "not_required" and not item.get("evidence"):
            eligibility = "unknown"
            events.append(
                _event(
                    "unproven_eligibility_exclusion",
                    "warning",
                    "资格排除缺少依据，仍纳入保守覆盖分母",
                    key,
                )
            )
        frozen = {**item, "eligibility": eligibility}
        (excluded if eligibility == "not_required" else planned).append(frozen)
    received, usable, eligible_usable = [], [], 0
    received_keys, returned_candidates, received_key_set = 0, 0, set()
    field_counts = {}
    for item in planned:
        key = item["key"]
        record = selected.get(key)
        state = latest.get(key)
        conflict_candidates = (
            state.evidence.get("candidate_source_version_ids", [])
            if state and state.state_status == "conflict"
            else []
        )
        if record or conflict_candidates:
            received_keys += 1
            received_key_set.add(key)
            returned_candidates += len(set(conflict_candidates)) if conflict_candidates else 1
        if not record:
            continue
        received.append(record)
        fields = USABLE_FIELDS.get(key.statement_type, ())
        if (
            fields
            and _valid_record(record)
            and all(_field(record, field)[0] is not None for field in fields)
        ):
            usable.append(record)
            eligible_usable += item["eligibility"] == "eligible"
        if not _valid_record(record):
            events.append(
                _event(
                    "record_unusable",
                    "error",
                    "记录币种、口径或源时间不可确认，保留证据并阻止依赖计算",
                    key,
                    record,
                    evidence={"reasons": record.reasons},
                )
            )
        for spec in FIELDS.get(key.statement_type, ()):
            status = record.field_states.get(spec.name, "invalid")
            if status == "not_applicable":
                continue
            field_key = f"{key.statement_type}.{spec.name}"
            counts = field_counts.setdefault(field_key, Counter())
            counts["denominator"] += 1
            counts["invalid"] += status == "invalid"
            counts["missing"] += status == "missing"
            counts["null"] += record.values.get(spec.name) is None
            if status in ("invalid", "warning"):
                events.append(
                    _event(
                        "financial_field_" + status,
                        "error" if status == "invalid" else "warning",
                        "财务字段存在硬错误"
                        if status == "invalid"
                        else "财务字段触发软校验，保留原值",
                        key,
                        record,
                        (spec.name,),
                        {"reason_codes": list(record.reasons.get(spec.name, ()))},
                    )
                )
        if key.statement_type == "balance":
            assets, liabilities, equity = (
                _field(record, name)[0]
                for name in ("total_assets", "total_liabilities", "total_equity")
            )
            if all(value is not None for value in (assets, liabilities, equity)) and abs(
                assets - liabilities - equity
            ) > max(1.0, abs(assets) * 1e-6):
                events.append(
                    _event(
                        "accounting_identity_mismatch",
                        "warning",
                        "资产不等于负债与权益之和，请核验范围、单位及舍入精度",
                        key,
                        record,
                        ("total_assets", "total_liabilities", "total_equity"),
                        {"absolute_difference": abs(assets - liabilities - equity)},
                    )
                )
            for part, whole in (
                ("current_assets", "total_assets"),
                ("current_liabilities", "total_liabilities"),
            ):
                left, right = _field(record, part)[0], _field(record, whole)[0]
                if (
                    left is not None
                    and right is not None
                    and left - right > max(1.0, abs(right) * 1e-6)
                ):
                    events.append(
                        _event(
                            "balance_component_exceeds_total",
                            "warning",
                            "流动科目超过对应总额，请核验范围、单位和修订版本",
                            key,
                            record,
                            (part, whole),
                            {"component": left, "total": right},
                        )
                    )
    denominator = len(planned)
    eligible_count = sum(item["eligibility"] == "eligible" for item in planned)
    unknown_count = sum(item["eligibility"] == "unknown" for item in planned)
    applicable_fields = sum(item["denominator"] for item in field_counts.values())
    invalid_fields = sum(item["invalid"] for item in field_counts.values())
    missing_fields = sum(item["missing"] for item in field_counts.values())
    null_fields = sum(item["null"] for item in field_counts.values())
    feature_keys = [(item.security_code, item.report_end, item.feature_name) for item in features]
    if len(set(feature_keys)) != len(feature_keys):
        blocking.append("duplicate_feature_key")
    applicable_features = [item for item in features if item.feature_status != "not_applicable"]
    valid_features = [
        item
        for item in applicable_features
        if item.feature_status in ("ok", "warning")
        and item.feature_value is not None
        and math.isfinite(item.feature_value)
    ]
    metrics = {
        "planned_count": denominator,
        "requested_count": len(seen),
        "not_required_count": len(excluded),
        "received_count": received_keys,
        "selected_record_count": len(received),
        "returned_candidate_count": returned_candidates,
        "accepted_count": sum(_valid_record(item) for item in received),
        "quarantined_count": sum(item.quality_status == "quarantined" for item in received),
        "usable_count": len(usable),
        "eligible_count": eligible_count,
        "unknown_count": unknown_count,
        "report_coverage_rate": _rate(received_keys, denominator),
        "usable_report_rate": _rate(len(usable), denominator),
        "conditional_usable_rate": _rate(eligible_usable, eligible_count),
        "unknown_eligibility_rate": _rate(unknown_count, denominator),
        "field_missing_rate": _rate(missing_fields, applicable_fields),
        "field_invalid_rate": _rate(invalid_fields, applicable_fields),
        "max_field_invalid_rate": max(
            (counts["invalid"] / counts["denominator"] for counts in field_counts.values()),
            default=None,
        ),
        "field_null_rate": _rate(null_fields, applicable_fields),
        "field_rates": {
            name: {
                **dict(counts),
                "invalid_rate": _rate(counts["invalid"], counts["denominator"]),
                "missing_rate": _rate(counts["missing"], counts["denominator"]),
            }
            for name, counts in sorted(field_counts.items())
        },
        "feature_count": len(features),
        "feature_value_count": len(valid_features),
        "feature_value_coverage_rate": _rate(len(valid_features), len(applicable_features)),
        "feature_reason_counts": dict(
            sorted(Counter(item.feature_status for item in features).items())
        ),
        "usable_required_fields": {name: list(fields) for name, fields in USABLE_FIELDS.items()},
    }
    metrics.update(_basis_coverage(features, records))
    if denominator == 0:
        blocking.append("zero_effective_scope")
    elif metrics["usable_report_rate"] < minimum:
        blocking.append("usable_report_rate_below_budget")
    if (
        metrics["max_field_invalid_rate"] is not None
        and metrics["max_field_invalid_rate"] > maximum_invalid
    ):
        blocking.append("field_invalid_rate_above_budget")
    baseline = _baseline(metrics, previous)
    metrics.update(baseline)
    if baseline["row_count_change"] is not None and abs(baseline["row_count_change"]) > 0.2:
        blocking.append("row_count_change_above_budget")
    if baseline["field_null_rate_change"] is not None and baseline["field_null_rate_change"] > 0.05:
        blocking.append("field_null_rate_change_above_budget")
    floor = profile.get("parameters", {}).get("amount_denominator", 1.0)
    reconciliation = _reconcile(selected, planned, floor, latest)
    compared = [item for item in reconciliation if item["result"] in ("matched", "mismatched")]
    metrics.update(
        comparison_count=len(compared),
        comparison_planned_count=len(reconciliation),
        comparison_coverage=_rate(len(compared), len(reconciliation)),
        reference_coverage=_rate(
            sum(item["right_value"] is not None for item in reconciliation), len(reconciliation)
        ),
        match_rate=_rate(sum(item["result"] == "matched" for item in compared), len(compared)),
        reconciliation_reason_counts=dict(
            sorted(Counter(item["result"] for item in reconciliation).items())
        ),
    )
    for item in reconciliation:
        if item["result"] == "mismatched":
            key = BusinessKey(**item["left_inputs"][0]["key"])
            events.append(
                _event(
                    "same_origin_reconciliation_mismatch",
                    "warning",
                    "同源金额与指标不一致，先核验期间、分母和修订口径",
                    key,
                    selected.get(key),
                    evidence={
                        "reconciliation_id": item["reconciliation_id"],
                        "absolute_difference": item["absolute_difference"],
                    },
                )
            )
    for reason in sorted(set(blocking)):
        event = _event(
            reason,
            "error",
            "数据集未通过完整发布门禁，请依据指标定位并重跑",
            evidence={"reason": reason},
        )
        event["gate_action"] = "block"
        events.append(event)
    anomaly_keys = {BusinessKey(**item["key"]) for item in events if item["key"]}
    metrics["record_anomaly_rate"] = _rate(len(anomaly_keys & received_key_set), received_keys)
    return {
        "passed": not blocking,
        "blocking_reasons": sorted(set(blocking)),
        "metrics": metrics,
        "events": events,
        "reconciliation": reconciliation,
    }
