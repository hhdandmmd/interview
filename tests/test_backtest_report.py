"""核验图表日期、百分数和参考口径，防止展示误导金融结果。"""

import copy
import csv
import json
from fractions import Fraction
from xml.etree import ElementTree

import pytest
from test_backtest import calculate, inputs

from finpipe.backtest.report import REFERENCE, chart_data, nav_chart, render_report
from finpipe.cli import main
from finpipe.exports import sha256

NS = {"s": "http://www.w3.org/2000/svg"}


def report(result=None):
    """展示夹具明确标为人工；金融计算沿用独立手算用例。"""
    return {
        **(calculate() if result is None else result),
        "signal_metadata": {"time_policy": "observed", "notice": "人工核验"},
        "market_metadata": {"adjustment": "synthetic", "notice": "人工报价"},
        "limitations": ["仅观察窗口端点"],
    }


def series(svg, name):
    return ElementTree.fromstring(svg).find(f".//s:g[@data-series='{name}']/s:polyline", NS)


def test_date_axis_uses_elapsed_days_instead_of_equal_window_spacing():
    signals, prices, calendar = inputs()
    calendar += ["2024-01-23", "2024-01-24"]
    prices += [
        {"security_code": row["security_code"], "trade_date": day, "close": 100}
        for row in signals
        for day in calendar[-2:]
    ]
    result = report(calculate(signals, prices, calendar))
    svg = nav_chart(result)
    points = [
        tuple(map(float, point.split(","))) for point in series(svg, "G1").get("points").split()
    ]
    assert len(points) == 3
    assert points[0][0] == 90
    assert points[-1][0] == 1160
    assert points[1][0] == pytest.approx(90 + 1070 * 5 / 21, abs=0.01)
    assert "2024-01-03" in svg and "2024-01-24" in svg
    assert "日期" in svg and "净值（初始值 = 1）" in svg


def test_reference_is_stock_weighted_not_group_weighted_and_has_same_fees():
    signals, prices, calendar = inputs()
    extra = {
        **signals[-1],
        "security_code": "600006.SH",
        "feature_value": 5,
        "feature_version_id": "v5",
    }
    signals.append(extra)
    prices += [
        {"security_code": extra["security_code"], "trade_date": day, "close": close}
        for day, close in zip(calendar, (1, 100, 999, 140), strict=True)
    ]
    result = report(calculate(signals, prices, calendar, cost_bps=10))
    reference = [row["nav"] for row in chart_data(result) if row["series"] == REFERENCE]
    assert reference == pytest.approx([1, float(Fraction(23, 20) * Fraction(999, 1000) ** 2)])
    average_groups = sum(row["net_return"] for row in result["period_returns"]) / 5
    assert reference[-1] - 1 != pytest.approx(average_groups)


def test_unknown_group_and_reference_nav_never_resume_after_missing_period():
    original = report()
    result = copy.deepcopy(original)
    result["nav"] = [
        {"exit_date": day, "group": group, "nav": value}
        for day, value in (("2024-01-08", None), ("2024-01-23", 1.3))
        for group in range(1, 6)
    ]
    result["period_returns"] += [
        {**row, "exit_date": "2024-01-23"} for row in original["period_returns"]
    ]
    for row in result["period_returns"][:5]:
        row["cohort_return"] = None
    assert [row["nav"] for row in chart_data(result) if row["series"] == REFERENCE] == [
        1,
        None,
        None,
    ]
    svg = nav_chart(result)
    assert len(series(svg, "G1").get("points").split()) == 1
    assert len(series(svg, REFERENCE).get("points").split()) == 1
    assert "2024-01-23 | 净值" not in svg
    assert original["nav"][0]["nav"] is not None


def test_no_formed_windows_show_empty_result_without_fabricated_flat_performance():
    signals, prices, calendar = inputs()
    for row in signals:
        row["available_at"] = "2024-01-02T15:00:01+08:00"
    result = report(calculate(signals, prices, calendar))
    assert chart_data(result) == []
    svg = nav_chart(result)
    assert "未形成有效持有窗口" in svg
    assert not ElementTree.fromstring(svg).findall(".//s:circle", NS)
    body = render_report(result, svg)
    assert "暂无有效分组成员" in body and "缺失" in body


@pytest.mark.parametrize("value", [0.0, 1.0, 1.000001, 2.5])
def test_constant_nav_has_finite_scale_and_visible_initial_baseline(value):
    result = report()
    for row in result["nav"]:
        row["nav"] = value
    svg = nav_chart(result)
    ElementTree.fromstring(svg)
    assert "nan" not in svg.lower() and "inf" not in svg.lower()
    assert "stroke-dasharray" in svg


def test_report_explains_factor_and_escapes_external_identifiers():
    result = report()
    result["parameters"]["feature"] = "roe_weighted_ytd"
    result["memberships"][0]["feature_value"] = 0.15
    body = render_report(result, nav_chart(result))
    assert "披露累计加权ROE" in body and "15.00%" in body
    assert "因子最低" in body and "因子最高" in body
    assert "不是市场指数" in body and "连线不代表逐日净值" in body
    assert "2024-01-03 至 2024-01-08" in body
    result["parameters"]["feature"] = '<script>alert("外部")</script>'
    body = render_report(result, nav_chart(result))
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_written_chart_and_csv_match_json_and_all_new_files_are_sealed(tmp_path, capsys):
    assert main(["backtest", "--demo", "--output", str(tmp_path / "报告")]) == 0
    capsys.readouterr()
    output = tmp_path / "报告"
    result = json.loads((output / "report.json").read_text(encoding="utf-8"))
    chart = (output / "nav.svg").read_text(encoding="utf-8")
    body = (output / "report.html").read_text(encoding="utf-8")
    assert chart in body
    with (output / "chart_data.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    expected = chart_data(result)
    assert len(rows) == len(expected)
    for actual, source in zip(rows, expected, strict=True):
        assert actual["trade_date"] == source["trade_date"] and actual["series"] == source["series"]
        assert float(actual["nav"]) == pytest.approx(source["nav"])
    hashes = json.loads((output / "SHA256.json").read_text(encoding="utf-8"))
    for name in ("nav.svg", "chart_data.csv", "report.html"):
        assert hashes[name] == sha256(output / name)
