"""简单入口的关键契约：自选公司、自动历史、失败状态及真实归档的完整因子链路。"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from finpipe.cli import main as cli
from finpipe.config import RunConfig, load_config
from finpipe.exports import sha256
from finpipe.runner import main, run
from finpipe.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def settings_file(
    directory, body='stocks=["600519.SH"], start_period="2025Q1", end_period="2025Q4"'
):
    """建立独立本地配置，相对结果目录应锚定此文件而不是启动位置。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.py"
    path.write_text(
        f'from finpipe.config import RunConfig\nCONFIG = RunConfig({body}, output_dir="result")\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "start,end,expected_first,expected_count",
    [
        ("2025Q1", "2025Q4", "2024-03-31", 8),
        ("2025Q3", "2025Q3", "2024-03-31", 7),
        ("2024Q4", "2025Q1", "2023-03-31", 9),
    ],
)
def test_simple_scope_keeps_requested_companies_and_exact_output_periods(
    tmp_path, start, end, expected_first, expected_count
):
    options = RunConfig(
        stocks=["300750.SZ", "600519.SH"], start_period=start, end_period=end, output_dir=tmp_path
    )
    prepared = options.to_pipeline_config()
    assert prepared["scope"]["codes"] == ["300750.SZ", "600519.SH"]
    assert len(prepared["input_periods"]) == expected_count
    assert prepared["input_periods"][0] == expected_first
    assert prepared["input_periods"][-1] == prepared["output_periods"][-1]
    assert prepared["_output_explicit"]
    assert options.stocks == ["300750.SZ", "600519.SH"]


@pytest.mark.parametrize("source", ["eastmoney", "东方财富"])
def test_run_reuses_online_pipeline_without_changing_default_pool(tmp_path, source):
    options = RunConfig(source=source, output_dir=tmp_path)
    result = {"status": "FAILED", "error": "人工断网反例"}
    with patch("finpipe.runner.execute_online", return_value=result) as online:
        assert run(options) is result
    assert online.call_args.args[0]["scope"]["codes"] == ["600519.SH"]
    assert len(load_config()["scope"]["codes"]) == 50


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "cninfo"},
        {"stocks": "600519.SH"},
        {"stocks": []},
        {"stocks": ["600519"]},
        {"stocks": ["600519.SH", "600519.SH"]},
        {"start_period": "2025Q4", "end_period": "2025Q1"},
        {"start_period": 2025},
        {"output_dir": ""},
        {"output_dir": None},
    ],
)
def test_bad_simple_config_stops_before_network_or_workspace_creation(tmp_path, changes):
    options = RunConfig(output_dir=tmp_path / "result")
    for name, value in changes.items():
        setattr(options, name, value)
    with patch("finpipe.runner.execute_online") as online, pytest.raises(ValueError):
        run(options)
    online.assert_not_called()
    assert not (tmp_path / "result").exists()


def test_file_and_memory_config_share_validation_and_cannot_be_combined(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[scope]\ncodes=["600519.SH"]\n', encoding="utf-8")
    a = load_config(path, tmp_path / "result")
    b = load_config(workspace=tmp_path / "result", values={"scope": {"codes": ["600519.SH"]}})
    assert a == b
    with pytest.raises(ValueError, match="不能同时"):
        load_config(path, values={})
    with pytest.raises(ValueError, match="对象"):
        load_config(values=[])


def test_config_path_is_stable_when_started_from_another_directory(tmp_path, monkeypatch, capsys):
    path = settings_file(tmp_path / "配置 中文 空格")
    monkeypatch.chdir(tmp_path)
    outcome = {
        "status": "PUBLISHED",
        "counts": {"scan_returned_reports": 24, "evaluated": 60, "reused": 0},
        "export_path": str(tmp_path / "报告"),
    }
    with patch("finpipe.runner.execute_online", return_value=outcome) as online:
        assert cli(["--settings", str(path)]) == 0
    assert online.call_args.args[0]["workspace"] == str(path.parent / "result")
    text = capsys.readouterr().out
    assert "东方财富" in text and "因子位置：60" in text and "2024-03-31" in text


def test_cli_no_arguments_loads_local_config_instead_of_fifty_default_companies(
    tmp_path, monkeypatch, capsys
):
    settings_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    with patch(
        "finpipe.runner.execute_online",
        return_value={"status": "FAILED", "error": "人工上游中断", "run_id": "test_run"},
    ) as online:
        assert cli([]) == 3
    assert online.call_args.args[0]["scope"]["codes"] == ["600519.SH"]
    text = capsys.readouterr().out
    assert "人工上游中断" in text and "恢复命令：& " in text and "--run-id 'test_run'" in text


@pytest.mark.parametrize("body", ["CONFIG = {", "CONFIG = 123", "CONFIG = RunConfig(unknown=1)"])
def test_malformed_python_config_has_chinese_error_without_pipeline_calls(tmp_path, capsys, body):
    path = tmp_path / "config.py"
    path.write_text("from finpipe.config import RunConfig\n" + body, encoding="utf-8")
    with patch("finpipe.runner.execute_online") as online:
        assert main(path) == 2
    online.assert_not_called()
    captured = capsys.readouterr()
    assert "运行失败" in captured.out and not captured.err


def test_published_but_stale_result_is_not_reported_as_success(tmp_path, capsys):
    with patch(
        "finpipe.runner.execute_online",
        return_value={"status": "PUBLISHED", "scope_health": "stale"},
    ):
        assert main(RunConfig(output_dir=tmp_path)) == 3
    text = capsys.readouterr().out
    assert "运行未完成" in text and "数据状态：stale" in text


def test_importing_root_main_has_no_network_and_run_uses_root_settings(tmp_path):
    specification = importlib.util.spec_from_file_location("finpipe_project_main", ROOT / "main.py")
    module = importlib.util.module_from_spec(specification)
    options = RunConfig(stocks=["300750.SZ"], output_dir=tmp_path / "result")
    # 配置属于用户可修改项；测试入口契约，不要求用户一直保留默认股票。
    with (
        patch("finpipe.runner.load_settings", return_value=options) as settings,
        patch("finpipe.runner.execute_online", return_value={"status": "ok"}) as online,
    ):
        specification.loader.exec_module(module)
        online.assert_not_called()
        settings.assert_not_called()
        assert module.run() == {"status": "ok"}
    settings.assert_called_once_with(ROOT / "config.py")
    assert online.call_args.args[0]["scope"]["codes"] == ["300750.SZ"]
    assert online.call_args.args[0]["workspace"] == str(tmp_path / "result")


def test_simple_api_replays_real_evidence_through_complete_online_processing(tmp_path):
    """替代采集器返回真实固定归档，未实际联网；保留真实观察时间并计算完整因子。"""
    manifest = ROOT / "src/finpipe/resources/demo/manifest.json"
    original_hash = sha256(manifest)
    options = RunConfig(
        stocks=["600519.SH", "000333.SZ", "000651.SZ"], output_dir=tmp_path / "隔离样例"
    )
    with patch("finpipe.ingestion.collect", return_value=manifest) as collect:
        first, second = run(options), run(options)
    assert collect.call_args.args[0] == tuple(sorted(options.stocks))
    assert collect.call_args.args[1][0] == "2024-03-31"
    assert first["status"] == "PUBLISHED" and first["counts"]["evaluated"] == 180
    assert second["status"] == "NO_CHANGE" and second["snapshot_id"] == first["snapshot_id"]
    assert second["counts"]["evaluated"] == 0 and second["counts"]["reused"] == 180
    assert (Path(first["export_path"]) / "report.html").exists()
    with Store(options.output_dir, readonly=True) as store:
        assert len(store.features(first["snapshot_id"])) == 180
    assert sha256(manifest) == original_hash
