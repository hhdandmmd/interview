"""独立手算收益与故障反例：防前视、失效回退、样本删除及复权口径偷换。"""

import json
from fractions import Fraction
from unittest.mock import patch

import pytest
from fixtures.financial_cases import write_manifest
from test_delivery_fixes import execute_at

from finpipe.backtest.calculation import run_groups, validate_signals
from finpipe.backtest.io import load_market, load_signals, load_workspace_signals, project_feature
from finpipe.backtest.market import decode_prices, fetch_market
from finpipe.backtest.sample import make_sample
from finpipe.cli import main
from finpipe.config import load_config
from finpipe.exports import sha256


def inputs():
    """五只人工证券、四个带假日间隔的人工交易日；回报直接手工指定。"""
    calendar = ["2024-01-02", "2024-01-03", "2024-01-05", "2024-01-08"]
    signals = [
        {
            "security_code": f"{600001 + i}.SH",
            "report_end": "2023-09-30",
            "feature_name": "test_factor",
            "feature_value": i,
            "feature_status": "ok",
            "feature_version_id": f"v{i}",
            "available_at": "2024-01-01T00:00:00Z",
        }
        for i in range(5)
    ]
    prices = [
        {"security_code": row["security_code"], "trade_date": day, "close": close}
        for i, row in enumerate(signals)
        for day, close in zip(calendar, (1, 100, 999, 90 + i * 10), strict=True)
    ]
    return signals, prices, calendar


def calculate(signals=None, prices=None, calendar=None, **kwargs):
    """默认持有两个交易日，不从实施结果构造预期。"""
    default = inputs()
    return run_groups(
        signals if signals is not None else default[0],
        prices if prices is not None else default[1],
        calendar if calendar is not None else default[2],
        feature="test_factor",
        horizon=2,
        **kwargs,
    )


def test_next_close_and_calendar_horizon_with_roundtrip_fees():
    result = calculate(cost_bps=10)
    assert result["status"] == "ok"
    for index, row in enumerate(result["period_returns"]):
        assert row["entry_date"] == "2024-01-03"
        assert row["exit_date"] == "2024-01-08"
        assert row["gross_return"] == pytest.approx(float(Fraction(index - 1, 10)))
        assert row["net_return"] == pytest.approx(
            float(Fraction(999, 1000) ** 2 * Fraction(9 + index, 10) - 1)
        )
        assert row["rank_ic"] == pytest.approx(1)


def test_after_close_signal_not_used_and_no_future_peek():
    signals, prices, calendar = inputs()
    for row in signals:
        row["available_at"] = "2024-01-02T15:00:01+08:00"
    result = calculate(signals, prices, calendar)
    assert result["status"] == "incomplete"
    assert result["summary"]["formed_periods"] == 0
    assert result["summary"]["exclusion_counts"] == {"not_yet_available": 5}
    assert all(row["cumulative_return"] is None for row in result["summary"]["groups"])


def test_invalid_latest_revision_does_not_revive_old_good_value():
    signals, prices, calendar = inputs()
    invalid = {
        **signals[0],
        "available_at": "2024-01-02T01:00:00Z",
        "feature_version_id": "bad",
        "feature_value": None,
        "feature_status": "version_conflict",
    }
    result = calculate([*signals, invalid], prices, calendar)
    assert result["summary"]["formed_periods"] == 0
    assert result["summary"]["exclusion_counts"] == {"version_conflict": 1}


def test_newer_report_wins_over_late_revision_of_old_quarter():
    signals, prices, calendar = inputs()
    newer = {
        **signals[0],
        "report_end": "2023-12-31",
        "feature_version_id": "new",
        "feature_value": 9,
    }
    older = {
        **signals[0],
        "available_at": "2024-01-02T01:00:00Z",
        "feature_version_id": "old_update",
        "feature_value": -9,
    }
    result = calculate([*signals, newer, older], prices, calendar)
    member = next(
        row for row in result["memberships"] if row["security_code"] == signals[0]["security_code"]
    )
    assert member["feature_version_id"] == "new"
    assert member["group"] == 5


@pytest.mark.parametrize("missing_day", ["2024-01-03", "2024-01-08"])
def test_missing_endpoint_keeps_member_and_invalidates_whole_group(missing_day):
    signals, prices, calendar = inputs()
    prices = [
        row
        for row in prices
        if not (row["security_code"] == "600001.SH" and row["trade_date"] == missing_day)
    ]
    result = calculate(signals, prices, calendar)
    assert result["status"] == "incomplete"
    assert len(result["memberships"]) == 5
    assert result["period_returns"][0]["net_return"] is None
    assert result["summary"]["groups"][0]["cumulative_return"] is None
    assert result["memberships"][0]["price_status"] == "missing_endpoint"


def test_missing_period_stops_nav_even_when_later_prices_recover():
    signals, prices, calendar, _ = make_sample()
    prices = [
        row
        for row in prices
        if not (row["security_code"] == "900001.SH" and row["trade_date"] == "2021-02-04")
    ]
    result = run_groups(signals, prices, calendar, feature="roe_weighted_ytd", horizon=2)
    assert result["summary"]["formed_periods"] == 5
    assert all(row["nav"] is None for row in result["nav"] if row["group"] == 1)
    assert result["period_returns"][10]["net_return"] is not None


def test_ties_have_deterministic_groups_and_null_rank_ic():
    signals, prices, calendar = inputs()
    for row in signals:
        row["feature_value"] = 1
    left, right = (
        calculate(signals, prices, calendar),
        calculate(list(reversed(signals)), prices, calendar),
    )
    assert left == right
    assert all(
        row["rank_ic"] is None and row["distinct_factor_values"] == 1
        for row in left["period_returns"]
    )


def test_declared_pool_includes_security_without_any_signal():
    result = calculate(
        codes=["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH", "600006.SH"]
    )
    assert result["summary"]["universe_size"] == 6
    assert result["exclusions"] == [
        {"decision_date": "2024-01-02", "security_code": "600006.SH", "reason": "not_yet_available"}
    ]


def test_actual_generated_roe_demo_matches_independent_two_day_returns():
    signals, prices, calendar, _ = make_sample()
    result = run_groups(signals, prices, calendar, feature="roe_weighted_ytd", horizon=2)
    expected = [
        Fraction(-1395, 100000),
        Fraction(-599, 100000),
        Fraction(2002, 1000000),
        Fraction(10026, 1000000),
        Fraction(18082, 1000000),
    ]
    assert result["status"] == "ok" and result["summary"]["formed_periods"] == 5
    for group, fraction in enumerate(expected, 1):
        assert all(
            row["gross_return"] == pytest.approx(float(fraction))
            for row in result["period_returns"]
            if row["group"] == group
        )
        assert result["summary"]["groups"][group - 1]["cumulative_return"] == pytest.approx(
            float((1 + fraction) ** 5 - 1)
        )
    assert [row["feature_value"] for row in result["memberships"][:10]] == pytest.approx(
        [0.04 * i for i in range(1, 11)]
    )


def test_publication_proxy_uses_latest_dependency_update_and_preserves_real_times():
    feature = {
        **inputs()[0][0],
        "data_available_at": "2026-10-13T00:00:00Z",
        "inputs": [
            {"source_available_at": "2023-10-25T16:00:00Z"},
            {"source_available_at": "2024-08-10T16:00:00Z"},
        ],
    }
    observed = project_feature(feature, "2026-10-13T01:00:00Z", "observed")
    approximate = project_feature(feature, "2026-10-13T01:00:00Z", "publication_proxy")
    assert observed["available_at"] == "2026-10-13T01:00:00.000000Z"
    assert approximate["available_at"] == "2024-08-10T16:00:00.000000Z"
    assert approximate["actual_data_available_at"] == "2026-10-13T00:00:00.000000Z"
    assert approximate["actual_published_at"] == observed["actual_published_at"]
    feature["inputs"][0]["source_available_at"] = None
    assert (
        project_feature(feature, "2026-10-13T01:00:00Z", "publication_proxy")["timing_status"]
        == "observed"
    )


@pytest.mark.parametrize(
    "mutation", ["duplicate_price", "negative_price", "bad_calendar", "ambiguous_signal", "nan"]
)
def test_input_contract_rejects_silent_errors(mutation):
    signals, prices, calendar = inputs()
    if mutation == "duplicate_price":
        prices.append(dict(prices[0]))
    elif mutation == "negative_price":
        prices[0]["close"] = -1
    elif mutation == "bad_calendar":
        calendar.reverse()
    elif mutation == "ambiguous_signal":
        signals.append({**signals[0], "feature_version_id": "conflict"})
    else:
        signals[0]["feature_value"] = float("nan")
    with pytest.raises(ValueError):
        calculate(signals, prices, calendar)


@pytest.mark.parametrize("kwargs", [{"groups": 1}, {"cost_bps": -1}, {"cost_bps": float("nan")}])
def test_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        calculate(**kwargs)


def market_body(field="hfqday"):
    """人工接口正文含范围外记录，用于验证列位置和过滤，不冒充真实报价。"""
    return json.dumps(
        {
            "code": 0,
            "data": {
                "sh600001": {
                    field: [
                        ["2023-12-29", "900", "100", "901", "99"],
                        ["2024-01-02", "900", "101", "901", "99"],
                        ["2024-01-03", "900", "102", "901", "99"],
                    ]
                }
            },
        }
    ).encode()


def test_market_range_column_and_hfq_contract():
    rows = decode_prices(
        b"fixture=" + market_body() + b";", "sh600001", "2024-01-01", "2024-01-31", "hfq"
    )
    assert rows == [
        {"trade_date": "2024-01-02", "close": 101},
        {"trade_date": "2024-01-03", "close": 102},
    ]
    with pytest.raises(ValueError, match="hfqday"):
        decode_prices(market_body("day"), "sh600001", "2024-01-01", "2024-01-31", "hfq")


def test_market_return_limit_detects_truncated_start():
    body = json.loads(market_body())
    from datetime import date, timedelta

    body["data"]["sh600001"]["hfqday"] = [
        [(date(2024, 1, 2) + timedelta(days=i)).isoformat(), "1", "1", "1", "1"] for i in range(640)
    ]
    with pytest.raises(ValueError, match="截断"):
        decode_prices(json.dumps(body).encode(), "sh600001", "2024-01-01", "2024-12-31", "hfq")


def test_failed_fetch_retains_error_manifest_and_is_not_consumable(tmp_path):
    with patch(
        "finpipe.backtest.market.SerialClient.get", return_value=(None, {"status": "request_error"})
    ):
        with pytest.raises(ValueError, match="未完成"):
            fetch_market(["600001.SH"], "2024-01-01", "2024-12-31", tmp_path / "failed", {})
    manifest = json.loads((tmp_path / "failed/manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False and manifest["error"] and manifest["requests"]
    with pytest.raises(ValueError, match="未完成"):
        load_market(tmp_path / "failed")


def test_offline_cli_report_repeat_with_exported_inputs_and_hash_failure(tmp_path, capsys):
    first, second = tmp_path / "人工 回测", tmp_path / "复算"
    assert main(["backtest", "--demo", "--output", str(first)]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "backtest",
                "--signals",
                str(first / "signals.json"),
                "--market",
                str(first / "market"),
                "--horizon",
                "2",
                "--output",
                str(second),
            ]
        )
        == 0
    )
    a = json.loads((first / "report.json").read_text(encoding="utf-8"))
    b = json.loads((second / "report.json").read_text(encoding="utf-8"))
    for field in ("memberships", "period_returns", "nav", "exclusions", "summary"):
        assert a[field] == b[field]
    assert main(["backtest", "--demo", "--output", str(first)]) == 2
    (first / "market/prices.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="摘要"):
        load_market(first / "market")


def test_external_observed_signals_cannot_erase_actual_timing(tmp_path):
    signal = {**inputs()[0][0], "actual_published_at": "2026-10-13T00:00:00Z"}
    path = tmp_path / "signals.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "metadata": {"kind": "fixture", "time_policy": "observed"},
                "rows": [signal],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不能早于"):
        load_signals(path, "observed")


def test_workspace_versions_keep_first_publication_and_bad_revision(tmp_path):
    """走真实发布存储：同一版本不改首次发布时间，坏新版保留并按时失效。"""
    workspace = tmp_path / "synthetic_workspace"
    config = load_config(workspace=workspace)
    first = execute_at(write_manifest(tmp_path / "first"), config, "2025-05-03T08:00:00.000000Z")
    old, _ = load_workspace_signals(workspace, first["snapshot_id"], "roe_weighted_ytd", "observed")

    def change(table, rows):
        if table == "indicator":
            for row in rows:
                if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith("2024-12-31"):
                    row["ROEJQ"] = None
                    row["_revision_sequence"] = 2
                    row["UPDATE_DATE"] = "2025-05-04 00:00:00"

    second = execute_at(
        write_manifest(tmp_path / "second", change, "2025-05-05T08:00:00.000000Z"),
        config,
        "2025-05-06T08:00:00.000000Z",
    )
    rows, _ = load_workspace_signals(
        workspace, second["snapshot_id"], "roe_weighted_ytd", "observed"
    )
    old_versions = {row["feature_version_id"]: row["available_at"] for row in old}
    assert all(
        row["available_at"] == old_versions[row["feature_version_id"]]
        for row in rows
        if row["feature_version_id"] in old_versions
    )
    changed = [
        row
        for row in rows
        if row["security_code"] == "600519.SH" and row["report_end"] == "2024-12-31"
    ]
    assert len(changed) == 2
    assert changed[0]["feature_value"] is not None and changed[1]["feature_value"] is None
    assert changed[1]["available_at"] == "2025-05-06T08:00:00.000000Z"
    assert (
        load_workspace_signals(workspace, first["snapshot_id"], "roe_weighted_ytd", "observed")[0]
        == old
    )


def test_report_period_cannot_be_known_before_its_shanghai_date():
    """外部错填的可用日期不能把尚未结束的财报送入历史截面。"""
    signals, _, _ = inputs()
    with pytest.raises(ValueError, match="报告期不能晚于"):
        calculate([{**row, "report_end": "2025-12-31"} for row in signals])
    signal = {**signals[0], "report_end": "2023-12-31"}
    validate_signals([{**signal, "available_at": "2023-12-30T16:00:00Z"}])
    validate_signals([{**signal, "available_at": "2023-12-31"}])
    with pytest.raises(ValueError, match="上海日期"):
        validate_signals([{**signal, "available_at": "2023-12-30T15:59:59Z"}])


def test_external_extra_fields_cannot_override_calculated_membership():
    signals, _, _ = inputs()
    rows = [
        {**row, "group": 99, "weight": 0, "entry_date": "1900-01-01", "decision_date": "bad"}
        for row in signals
    ]
    result = calculate(rows)
    for member, period in zip(result["memberships"], result["period_returns"], strict=True):
        assert member["group"] == period["group"]
        assert member["entry_date"] == period["entry_date"]
        assert member["decision_date"] == period["decision_date"]
        assert member["weight"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema_version": 1, "metadata": None, "rows": []},
        {
            "schema_version": 1,
            "metadata": {"kind": "fixture", "time_policy": "observed"},
            "rows": None,
        },
        {
            "schema_version": True,
            "metadata": {"kind": "fixture", "time_policy": "observed"},
            "rows": [],
        },
    ],
)
def test_external_signal_structure_errors_are_controlled_cli_failures(tmp_path, capsys, payload):
    path = tmp_path / "signals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    code = main(
        [
            "backtest",
            "--signals",
            str(path),
            "--market",
            str(tmp_path / "unused"),
            "--output",
            str(tmp_path / "report"),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["status"] == "FAILED"
    assert any("\u4e00" <= char <= "\u9fff" for char in result["error"])
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("security_code", 600001),
        ("feature_status", []),
        ("available_at", 2024),
        ("actual_published_at", {}),
        ("feature_value", 10**1000),
    ],
)
def test_external_signal_value_types_fail_at_input_boundary(field, value):
    with pytest.raises(ValueError):
        validate_signals([{**inputs()[0][0], field: value}])


@pytest.mark.parametrize(
    "payload",
    [[], None, {"code": 0, "data": []}, {"code": 0, "data": {"sh000001": []}}],
)
def test_market_schema_failure_preserves_context_and_closes_client(tmp_path, payload):
    output = tmp_path / "failed"
    with (
        patch(
            "finpipe.backtest.market.SerialClient.get",
            return_value=(json.dumps(payload).encode(), {"fixture": True}),
        ),
        patch("finpipe.backtest.market.SerialClient.close") as close,
    ):
        with pytest.raises(ValueError, match="采集未完成"):
            fetch_market(["600001.SH"], "2024-01-01", "2024-01-31", output, {})
    close.assert_called_once()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False
    assert manifest["error"]["type"] == "ValueError" and manifest["requests"]
    with pytest.raises(ValueError, match="未完成"):
        load_market(output)


def test_market_client_closes_even_if_final_manifest_write_fails(tmp_path):
    """磁盘故障不能跳过连接释放；此处不伪造已经落盘的错误清单。"""
    with (
        patch("finpipe.backtest.market.SerialClient.get", return_value=(None, {"fixture": True})),
        patch("finpipe.backtest.market.write_json", side_effect=OSError("人工磁盘故障")),
        patch("finpipe.backtest.market.SerialClient.close") as close,
    ):
        with pytest.raises(OSError, match="人工磁盘故障"):
            fetch_market(["600001.SH"], "2024-01-01", "2024-01-31", tmp_path / "failed", {})
    close.assert_called_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("codes", "600001.SH"),
        ("codes", [600001]),
        ("files", None),
        ("calendar_kind", []),
        ("start", 20240101),
    ],
)
def test_market_manifest_invalid_types_are_rejected_before_use(tmp_path, capsys, field, value):
    assert main(["backtest", "--demo", "--output", str(tmp_path / "base")]) == 0
    capsys.readouterr()
    market = tmp_path / "base/market"
    path = market / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        load_market(market)


def test_existing_market_output_gives_actionable_error_and_remains_untouched(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("人工既有证据", encoding="utf-8")
    with pytest.raises(ValueError, match="使用新的目录"):
        fetch_market(["600001.SH"], "2024-01-01", "2024-01-31", target, {})
    assert (target / "keep.txt").read_text(encoding="utf-8") == "人工既有证据"


@pytest.mark.parametrize(
    "name,body",
    [
        ("prices.csv", "security_code,trade_date\n600001.SH,2024-01-02\n"),
        ("prices.csv", "security_code,trade_date,close,close\n600001.SH,2024-01-02,1,2\n"),
        ("prices.csv", "security_code,trade_date,close\n600001.SH,2024-01-02,\n"),
        ("prices.csv", "security_code,trade_date,close\n600001.SH,2024-01-02,1,unexpected\n"),
        ("calendar.csv", "wrong_date_column\n2024-01-02\n"),
        ("calendar.csv", "trade_date\n2024-01-02,unexpected\n"),
    ],
)
def test_bad_csv_structure_rejected_even_with_matching_hash(tmp_path, capsys, name, body):
    """摘要证明字节未改，不证明业务结构正确，两者分别校验。"""
    assert main(["backtest", "--demo", "--output", str(tmp_path / "base")]) == 0
    capsys.readouterr()
    market = tmp_path / "base/market"
    target = market / name
    target.write_text(body, encoding="utf-8-sig")
    manifest_path = market / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][name] = sha256(target)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.csv"):
        load_market(market)
