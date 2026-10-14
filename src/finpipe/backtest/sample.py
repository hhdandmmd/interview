"""离线教学样例：人工财报经过现有清洗与特征公式，价格亦为人工构造。"""

from datetime import date, timedelta

from finpipe.features import compute_features
from finpipe.identity import stable_id
from finpipe.models import BusinessKey, RecordState, SourceRecord
from finpipe.transforms import normalize


def make_sample() -> tuple[list[dict], list[dict], list[str], dict]:
    """十家虚构证券的ROE与收益递增，仅验证计算与运行，不能证明因子有效。"""
    codes = tuple(f"{900001 + i:06d}.SH" for i in range(10))
    observed = "2021-01-25T00:00:00.000000Z"
    processed = "2021-01-25T00:00:01.000000Z"
    records, states = [], []
    profile = {
        "mapping_version": "1",
        "cleaning_rule_version": "1",
        "time_policy_version": "1",
        "formula_version": "2",
        "dictionary_version": "1",
    }
    for company, code in enumerate(codes):
        for year in (2019, 2020):
            for quarter, suffix in enumerate(("03-31", "06-30", "09-30", "12-31"), 1):
                period = f"{year}-{suffix}"
                notice = (date.fromisoformat(period) + timedelta(days=20)).isoformat()
                revenue = 100 * quarter * (1 + company / 10)
                for table in ("income", "balance", "indicator"):
                    key = BusinessKey("synthetic_backtest", code, period, table)
                    raw = {
                        "SECUCODE": code,
                        "REPORT_DATE": period,
                        "CURRENCY": "CNY",
                        "NOTICE_DATE": notice,
                        "UPDATE_DATE": notice,
                        "_evidence": {"fixture": True, "consolidation_scope": "consolidated"},
                    }
                    if table == "income":
                        raw.update(
                            OPERATE_INCOME=revenue,
                            TOTAL_OPERATE_INCOME=revenue,
                            OPERATE_COST=revenue * 0.6,
                            NETPROFIT=revenue * 0.1,
                            PARENT_NETPROFIT=revenue * 0.08,
                        )
                    elif table == "balance":
                        raw.update(
                            TOTAL_ASSETS=1000,
                            TOTAL_LIABILITIES=300,
                            TOTAL_EQUITY=700,
                            TOTAL_PARENT_EQUITY=700,
                            TOTAL_CURRENT_ASSETS=300,
                            TOTAL_CURRENT_LIAB=100,
                        )
                    else:
                        raw.update(
                            ROEJQ=(company + 1) * quarter, LD=3, ZCFZL=30, XSJLL=10, XSMLL=40
                        )
                    source_id = stable_id("synthetic_source", raw)
                    records.append(
                        normalize(SourceRecord(source_id, key, raw, observed), profile, processed)
                    )
                    states.append(
                        RecordState(
                            stable_id("synthetic_state", raw), key, "selected", source_id, processed
                        )
                    )
    features = compute_features(
        records,
        states,
        codes,
        ("2020-09-30", "2020-12-31"),
        "2021-01-26T00:00:00.000000Z",
        "synthetic_profile",
        "synthetic_scope",
    )
    signals = [
        {
            "security_code": item.security_code,
            "report_end": item.report_end,
            "feature_name": item.feature_name,
            "feature_value": item.feature_value,
            "feature_status": item.feature_status,
            "feature_version_id": item.feature_version_id,
            "available_at": item.data_available_at,
            "actual_data_available_at": item.data_available_at,
            "actual_published_at": processed,
        }
        for item in features
        if item.feature_name == "roe_weighted_ytd"
    ]
    calendar = [f"2021-02-{day:02d}" for day in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16)]
    prices = [
        {"security_code": code, "trade_date": day, "close": 10 * (1 + 0.002 * (i - 4)) ** index}
        for i, code in enumerate(codes)
        for index, day in enumerate(calendar)
    ]
    return (
        signals,
        prices,
        calendar,
        {
            "kind": "synthetic",
            "time_policy": "observed",
            "historical_versions_verified": False,
            "notice": "人工财报、证券、价格与日期序列；日期并非真实交易日历。收益趋势人为设定。",
            "feature_generation": "normalize -> compute_features，沿用主项目ROE公式",
        },
    )
