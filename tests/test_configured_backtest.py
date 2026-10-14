"""配置回测的真实编排边界；财务与行情夹具均明确人工，常规测试不联网。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fixtures.financial_cases import write_manifest
from test_delivery_fixes import execute_at
from test_finance import CODE, CUTOFF, fixture

from finpipe.backtest.io import run_backtest
from finpipe.backtest.market import write_csv
from finpipe.config import BacktestConfig, RunConfig
from finpipe.exports import sha256, write_json
from finpipe.features import compute_features
from finpipe.features.registry import FACTORS
from finpipe.runner import load_settings, main, run
from finpipe.runtime import result_exit_code
from finpipe.storage import Store


def artificial_market(path):
    """两个有报价的人工证券和四个人工交易日；第三只只用于验证显式股票池筛选。"""
    path.mkdir(parents=True)
    days = ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06"]
    prices = [
        {"security_code": code, "trade_date": day, "close": close}
        for code, values in (("600519.SH", (100, 100, 105, 110)), ("000333.SZ", (100, 100, 95, 90)))
        for day, close in zip(days, values, strict=True)
    ]
    write_csv(path / "prices.csv", prices, ["security_code", "trade_date", "close"])
    write_csv(path / "calendar.csv", [{"trade_date": day} for day in days], ["trade_date"])
    write_json(
        path / "manifest.json",
        {
            "schema_version": 1,
            "complete": True,
            "kind": "synthetic",
            "adjustment": "synthetic",
            "calendar_kind": "synthetic",
            "codes": ["600519.SH", "000333.SZ", "000651.SZ"],
            "start": days[0],
            "end": days[-1],
            "files": {name: sha256(path / name) for name in ("prices.csv", "calendar.csv")},
        },
    )
    return path


@pytest.fixture
def configured_case(tmp_path):
    """真实调用财务处理与发布，仅用人工来源证据和人工行情隔离验证回测编排。"""
    options = RunConfig(
        stocks=["600519.SH", "000333.SZ"],
        start_period="2024Q1",
        end_period="2024Q4",
        output_dir=tmp_path / "人工财务",
        backtest=BacktestConfig(
            enabled=True,
            factors=["roe_weighted_ytd", "debt_asset_ratio"],
            groups=2,
            horizon=2,
            time_policy="publication_proxy",
            start_date="2024-06-03",
            end_date="2024-06-06",
            market_dir=artificial_market(tmp_path / "人工行情"),
        ),
    )
    manifest = write_manifest(tmp_path / "人工原文")
    financial = execute_at(manifest, options.to_pipeline_config(), "2025-05-03T08:00:00.000000Z")
    assert financial["status"] == "PUBLISHED", financial
    return options, financial


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "yes"},
        {"factors": []},
        {"factors": "roe_weighted_ytd"},
        {"factors": ["roe_weighted_ytd", "roe_weighted_ytd"]},
        {"factors": ["unknown"]},
        {"groups": True},
        {"groups": 1},
        {"horizon": 0},
        {"horizon": True},
        {"cost_bps": float("nan")},
        {"time_policy": "pit"},
        {"start_date": "2024-06-31"},
        {"start_date": "2025-01-01", "end_date": "2024-01-01"},
        {"market_dir": ""},
    ],
)
def test_invalid_backtest_stops_before_financial_io(tmp_path, changes):
    backtest = BacktestConfig(enabled=True, groups=2)
    for key, value in changes.items():
        setattr(backtest, key, value)
    options = RunConfig(
        stocks=["600519.SH", "000333.SZ"], output_dir=tmp_path / "result", backtest=backtest
    )
    with patch("finpipe.runner.execute_online") as online, pytest.raises(ValueError):
        run(options)
    online.assert_not_called()
    assert not (tmp_path / "result").exists()


def test_five_groups_require_five_stocks_before_network(tmp_path):
    with (
        patch("finpipe.runner.execute_online") as online,
        pytest.raises(ValueError, match="至少需要5只"),
    ):
        run(RunConfig(output_dir=tmp_path / "result", backtest=BacktestConfig(enabled=True)))
    online.assert_not_called()


def test_disabled_backtest_and_failed_financial_stage_never_request_market(tmp_path):
    with (
        patch("finpipe.backtest.batch.run_selected") as selected,
        patch("finpipe.runner.execute_online", return_value={"status": "ok"}),
    ):
        assert run(RunConfig(output_dir=tmp_path))["status"] == "ok"
    selected.assert_not_called()
    options = RunConfig(
        stocks=["600519.SH", "000333.SZ"],
        output_dir=tmp_path,
        backtest=BacktestConfig(enabled=True, groups=2),
    )
    with (
        patch("finpipe.backtest.batch.run_selected") as selected,
        patch("finpipe.runner.execute_online", return_value={"status": "FAILED"}),
    ):
        assert run(options)["status"] == "FAILED"
    selected.assert_not_called()


def test_multi_factor_reports_use_pinned_snapshot_one_market_and_manual_returns(configured_case):
    options, financial = configured_case
    original = sha256(Path(options.output_dir) / "catalog.sqlite")
    from finpipe.backtest.io import load_market

    with (
        patch("finpipe.runner.execute_online", return_value=dict(financial)),
        patch("finpipe.backtest.batch.load_market", wraps=load_market) as market,
        patch("finpipe.backtest.batch.fetch_market") as fetch,
    ):
        result = run(options)
    market.assert_called_once()
    fetch.assert_not_called()
    batch = result["backtest"]
    assert batch["status"] == "ok" and len(batch["results"]) == 2
    assert Path(batch["report"]).is_file() and result_exit_code(result) == 0
    for index, item in enumerate(batch["results"]):
        report = json.loads((Path(item["output"]) / "report.json").read_text(encoding="utf-8"))
        assert report["parameters"]["feature"] == options.backtest.factors[index]
        assert report["signal_metadata"]["snapshot_id"] == financial["snapshot_id"]
        assert report["market_metadata"]["codes"] == options.stocks
        expected = (-0.1, 0.1) if index == 0 else (0.1, -0.1)
        assert [row["net_return"] for row in report["period_returns"]] == pytest.approx(expected)
        assert report["summary"]["formed_periods"] == 1
    with patch("finpipe.runner.execute_online", return_value=dict(financial)):
        repeated = run(options)
    assert repeated["backtest"]["output"] != batch["output"]
    assert sha256(Path(options.output_dir) / "catalog.sqlite") == original


def test_auto_market_fetches_once_for_all_factors(configured_case):
    options, financial = configured_case
    from finpipe.backtest.io import load_market

    data = load_market(options.backtest.market_dir)
    options.backtest.market_dir = None
    with (
        patch("finpipe.runner.execute_online", return_value=dict(financial)),
        patch("finpipe.backtest.batch.fetch_market", return_value={"status": "ok"}) as fetch,
        patch("finpipe.backtest.batch.load_market", return_value=data),
    ):
        result = run(options)
    assert result["backtest"]["status"] == "ok"
    fetch.assert_called_once()
    assert fetch.call_args.args[:3] == (options.stocks, "2024-06-03", "2024-06-06")


def test_one_factor_failure_keeps_other_report_and_financial_head(configured_case, capsys):
    options, financial = configured_case

    def fail_second(*args, **kwargs):
        if kwargs["feature"] == "debt_asset_ratio":
            raise ValueError("人工注入的第二因子报告失败")
        return run_backtest(*args, **kwargs)

    with (
        patch("finpipe.runner.execute_online", return_value=dict(financial)),
        patch("finpipe.backtest.batch.run_backtest", side_effect=fail_second),
    ):
        assert main(options) == 3
    text = capsys.readouterr().out
    assert "财报结果已保留" in text and "人工注入" in text
    with Store(options.output_dir, readonly=True) as store:
        assert store.select_snapshot()["snapshot_id"] == financial["snapshot_id"]
    batch = json.loads(
        next((Path(options.output_dir) / "backtests").glob("*/batch.json")).read_text(
            encoding="utf-8"
        )
    )
    assert [item["status"] for item in batch["results"]] == ["ok", "FAILED"]
    assert Path(batch["results"][0]["report"]).is_file()


def test_observed_historical_empty_result_is_reported_as_incomplete(configured_case):
    options, financial = configured_case
    options.backtest.time_policy = "observed"
    with patch("finpipe.runner.execute_online", return_value=dict(financial)):
        result = run(options)
    assert result["status"] == "PUBLISHED" and result["backtest"]["status"] == "incomplete"
    assert result_exit_code(result) == 3
    assert all(item["summary"]["formed_periods"] == 0 for item in result["backtest"]["results"])


def test_local_market_path_is_anchored_to_settings_file(tmp_path, monkeypatch):
    path = tmp_path / "配置 中文" / "config.py"
    path.parent.mkdir()
    path.write_text(
        'from finpipe.config import RunConfig, BacktestConfig\nCONFIG=RunConfig(backtest=BacktestConfig(market_dir="行情"))\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert load_settings(path).backtest.market_dir == path.parent / "行情"


def test_market_failure_preserves_financial_and_records_batch_context(configured_case):
    options, financial = configured_case
    options.backtest.market_dir = None
    with (
        patch("finpipe.runner.execute_online", return_value=dict(financial)),
        patch("finpipe.backtest.batch.fetch_market", side_effect=ValueError("人工行情中断")),
    ):
        result = run(options)
    assert result["status"] == "PUBLISHED" and result_exit_code(result) == 3
    batch = result["backtest"]
    assert batch["status"] == "FAILED" and "人工行情中断" in batch["error"]
    assert Path(batch["report"]).is_file() and Path(batch["output"], "batch.json").is_file()
    assert batch["snapshot_id"] == financial["snapshot_id"]


def test_external_synthetic_market_still_checks_declared_date_coverage(tmp_path):
    from finpipe.backtest.io import load_market

    market = load_market(artificial_market(tmp_path / "人工行情"))
    with pytest.raises(ValueError, match="覆盖范围"):
        run_backtest(
            [],
            {},
            market,
            feature="roe_weighted_ytd",
            output=tmp_path / "report",
            start="2024-01-01",
        )
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize("period", ["2020-03-31", "2020-09-30"])
def test_factor_declared_dependencies_match_real_lineage(period):
    """独立展开声明期数，发现新增因子的公式与依赖声明不一致。"""
    records, states = fixture()
    results = compute_features(records, states, (CODE,), (period,), CUTOFF, "人工口径", "人工范围")
    for item in results:
        definition = FACTORS[item.feature_name]
        declared = set()
        for table, field, basis, offsets in definition.dependencies:
            for offset in offsets:
                year, quarter = divmod(int(period[:4]) * 4 + int(period[5:7]) // 3 - 1 - offset, 4)
                target = f"{year:04d}-" + ("03-31", "06-30", "09-30", "12-31")[quarter]
                declared.add((table, field, target))
                if basis == "quarter" and quarter > 0:
                    previous = f"{year:04d}-" + ("03-31", "06-30", "09-30", "12-31")[quarter - 1]
                    declared.add((table, field, previous))
        assert {
            (dep.key.statement_type, dep.field_name, dep.key.report_end) for dep in item.inputs
        } == declared
        assert (
            definition.calculate.__module__
            == f"finpipe.features.factors.{item.feature_name}.factor"
        )
