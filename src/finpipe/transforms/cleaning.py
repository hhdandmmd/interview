"""按明确来源规格清洗财报；局部字段错误不抹去无关财务值。"""

import math
import re
from datetime import date, datetime, time, timedelta

from finpipe.catalog import FIELDS
from finpipe.identity import stable_id
from finpipe.models import CleanRecord, SourceRecord
from finpipe.timeutil import SHANGHAI, UTC, iso_time, parse_time, quarter_end


def _number(raw: object, multiplier: float) -> tuple[float | None, str, tuple[str, ...]]:
    """明确空值与非法值；人民币金额不能猜测亿、万元等未声明单位。"""
    if raw is None:
        return None, "missing", ("source_null",)
    if isinstance(raw, str) and raw.strip() in ("", "--", "—", "未披露", "不适用"):
        return None, "missing", ("source_placeholder",)
    if isinstance(raw, bool):
        return None, "invalid", ("unparseable_number",)
    try:
        value = float(raw) * multiplier
    except (TypeError, ValueError, OverflowError):
        return None, "invalid", ("unparseable_number",)
    if not math.isfinite(value):
        return None, "invalid", ("nonfinite_number",)
    return value if value else 0.0, "ok", ()


def _source_time(raw: object, precision: str, observed: datetime, report_end: str) -> str:
    """先校验来源事实，再将日期精度映射到上海次日零点。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("缺少来源时间")
    value = raw.strip()
    if precision == "date":
        if len(value) > 10:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        source_day = date.fromisoformat(value[:10])
        if (
            source_day < date.fromisoformat(report_end)
            or source_day > observed.astimezone(SHANGHAI).date()
        ):
            raise ValueError("来源日期早于报告期或晚于观察日")
        projected = datetime.combine(source_day + timedelta(days=1), time.min, SHANGHAI)
    elif precision == "timestamp":
        projected = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if projected.tzinfo is None:
            projected = projected.replace(tzinfo=SHANGHAI)
        if (
            projected.astimezone(SHANGHAI).date() < date.fromisoformat(report_end)
            or projected.astimezone(UTC) > observed
        ):
            raise ValueError("来源时刻早于报告期或晚于观察时刻")
    else:
        raise ValueError("来源时间精度未声明")
    return projected.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize(source: SourceRecord, profile: dict, processed_at: str) -> CleanRecord:
    """输出元/小数/倍数及字段状态；真实观察时间与处理时间各自保留。"""
    if profile.get("time_policy_version") != "1":
        raise ValueError("不支持此时间政策版本；不能改标签后沿用旧算法")
    if len(source.first_seen_at) == 10 or len(processed_at) == 10:
        raise ValueError("真实观察及处理时刻必须为带时区的精确时间，不能用日期补造")
    key, raw = source.key, source.raw
    evidence = raw.get("_evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
    values, states, reasons = {}, {}, {}
    flags, record_errors, time_reasons = [], [], []
    first_seen = iso_time(source.first_seen_at)
    observed = parse_time(first_seen)
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", key.security_code):
        record_errors.append("invalid_security_identity")
    try:
        if quarter_end(key.report_end) != key.report_end:
            record_errors.append("invalid_report_period")
    except (ValueError, TypeError):
        record_errors.append("invalid_report_period")
    if raw.get("SECUCODE", key.security_code) != key.security_code:
        record_errors.append("invalid_security_identity")
    if raw.get("REPORT_DATE") is not None and str(raw["REPORT_DATE"])[:10] != key.report_end:
        record_errors.append("invalid_report_period")
    if key.statement_type not in FIELDS:
        record_errors.append("unsupported_source_contract")
    if (
        key.consolidation_scope != "consolidated"
        or evidence.get("consolidation_scope") != "consolidated"
    ):
        record_errors.append("unknown_scope")
    currency = raw.get("CURRENCY")
    if (
        currency is None
        and evidence.get("currency_assumed") == "CNY"
        and evidence.get("currency_evidence")
    ):
        flags.append("currency_source_spec_assumed")
    elif currency not in ("CNY", "RMB", "人民币", "人民币元"):
        record_errors.append("unknown_currency")
    if raw.get("ORG_TYPE") in ("银行", "保险", "证券", "金融"):
        record_errors.append("unsupported_financial_issuer")
    if evidence.get("source_timezone", "Asia/Shanghai") != "Asia/Shanghai":
        record_errors.append("unsupported_source_timezone")

    # 公告证据不可由更新日期补位；坏日期保留真实观察边界，阻止旧值复活。
    source_available = None
    try:
        if "invalid_report_period" in record_errors:
            raise ValueError("报告期无效")
        disclosed = _source_time(
            raw.get("NOTICE_DATE"),
            evidence.get("disclosed_precision", "date"),
            observed,
            key.report_end,
        )
        updated_raw = raw.get("UPDATE_DATE")
        if updated_raw is None or (
            isinstance(updated_raw, str) and updated_raw.strip() in ("", "--")
        ):
            time_reasons.append("missing_update_evidence")
            source_available = disclosed
        else:
            updated = _source_time(
                updated_raw, evidence.get("updated_precision", "date"), observed, key.report_end
            )
            source_available = max(disclosed, updated)
        time_valid = True
    except (ValueError, TypeError, OverflowError):
        time_valid = False
        time_reasons.append("invalid_time_evidence")
        if raw.get("NOTICE_DATE") is None:
            time_reasons.append("missing_disclosure_evidence")

    for spec in FIELDS.get(key.statement_type, ()):
        if spec.source_field not in raw:
            value, state, why = None, "invalid", ("unsupported_source_contract",)
        else:
            value, state, why = _number(raw[spec.source_field], spec.multiplier)
        if value is not None and (
            (spec.name == "total_assets" and value <= 0)
            or (
                spec.name in ("total_liabilities", "current_assets", "current_liabilities")
                and value < 0
            )
        ):
            value, state, why = None, "invalid", ("impossible_financial_value",)
        if value is not None and spec.name == "roe_weighted_ytd" and not -1 <= value <= 1:
            state, why = "warning", ("extreme_roe",)
        values[spec.name], states[spec.name], reasons[spec.name] = value, state, why

    if key.statement_type == "balance" and all(
        values.get(name) is not None
        for name in ("total_assets", "total_liabilities", "total_equity")
    ):
        assets, liabilities, equity = (
            values[name] for name in ("total_assets", "total_liabilities", "total_equity")
        )
        if abs(assets - liabilities - equity) > max(1.0, abs(assets) * 1e-6):
            for name in ("total_assets", "total_liabilities", "total_equity"):
                states[name] = "warning"
                reasons[name] += ("accounting_identity_mismatch",)

    # 可比性必须由对应期间的证据支持，普通缺核验不能冒充已知重述。
    basis = raw.get("_basis_status", "source_assumed")
    if basis == "restatement_unresolved":
        basis = "known_risk_unresolved"
    if basis == "verified_comparable" and not evidence.get("basis_evidence"):
        basis = "source_assumed"
        flags.append("unverified_basis_claim")
    if basis not in (
        "verified_comparable",
        "source_assumed",
        "known_risk_unresolved",
        "incompatible",
    ):
        basis = "source_assumed"
        flags.append("unknown_basis_status")
    if time_reasons:
        reasons["__time__"] = tuple(time_reasons)
    if record_errors:
        reasons["__record__"] = tuple(sorted(set(record_errors)))
    if record_errors:
        quality = "quarantined"
    elif not time_valid or time_reasons or flags or any(state != "ok" for state in states.values()):
        quality = "accepted_with_issues"
    else:
        quality = "accepted"
    clean_id = stable_id(
        "clean",
        {
            "source_version_id": source.source_version_id,
            "mapping_version": profile["mapping_version"],
            "cleaning_rule_version": profile["cleaning_rule_version"],
        },
    )
    data_available = (
        first_seen
        if quality == "quarantined" or not time_valid
        else max(first_seen, source_available)
    )
    return CleanRecord(
        clean_id,
        source.source_version_id,
        key,
        values,
        states,
        reasons,
        quality,
        source_available,
        data_available,
        iso_time(processed_at),
        time_valid,
        basis,
        tuple(flags),
    )
