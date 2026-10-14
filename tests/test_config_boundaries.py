"""配置与来源结构的实际误用反例，必须可定位失败且不创建业务产物。"""

import json
from unittest.mock import patch

import pytest

from finpipe.catalog import FIELDS
from finpipe.cli import main
from finpipe.config import load_config
from finpipe.ingestion import ManifestError, load_manifest
from finpipe.ingestion.archive import decode_page
from finpipe.reconciliation import compare_reference
from finpipe.storage import Store
from finpipe.timeutil import utc_now


@pytest.mark.parametrize(
    "content",
    [
        "scope=1\n",
        "workspace=42\n",
        'workspace=""\n',
        '[scope]\ncodes=[["600519.SH"]]\n',
        "[scope]\ninput_start=2022\n",
        "[profile]\ncleaning_rule_version=[]\n",
        '[profile]\ncleaning_rule_version=""\n',
    ],
)
def test_config_wrong_types_return_actionable_json_before_writes(tmp_path, capsys, content):
    """错误必须在配置边界发现，不能泄露Python堆栈或先创建报告目录。"""
    config = tmp_path / "bad.toml"
    config.write_text(content, encoding="utf-8")
    output = tmp_path / "result"
    assert main(["backtest", "--demo", "--config", str(config), "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.err
    assert json.loads(captured.out)["status"] == "FAILED"
    assert not output.exists()


def test_remove_ignored_sample_size_and_keep_real_cli_selection(tmp_path, capsys):
    """样本数只在有效CLI入口设置，配置中的无效旧字段不能继续静默忽略。"""
    config = tmp_path / "ignored.toml"
    config.write_text("[scope]\nsample_size=3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="scope.sample_size"):
        load_config(config)
    with patch("finpipe.cli.execute_online", return_value={"status": "ok"}) as online:
        assert main(["run", "--sample-size", "3", "--workspace", str(tmp_path / "workspace")]) == 0
    assert len(online.call_args.args[0]["scope"]["codes"]) == 3
    capsys.readouterr()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {
            "schema_version": 1,
            "source": "eastmoney",
            "capture_kind": "real",
            "complete": False,
            "codes": [["600519.SH"]],
            "periods": ["2025-12-31"],
            "pages": [],
        },
    ],
)
def test_malformed_financial_manifest_is_explicit_input_error(tmp_path, payload):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_financial_identity_wrong_type_is_source_contract_error():
    body = json.dumps(
        {
            "success": True,
            "result": {
                "count": 1,
                "pages": 1,
                "data": [
                    {
                        "SECUCODE": [],
                        "REPORT_DATE": "2025-12-31",
                        "NOTICE_DATE": "2026-04-01",
                        **{spec.source_field: 100 for spec in FIELDS["income"]},
                    }
                ],
            },
        }
    ).encode()
    with pytest.raises(ManifestError, match="字符串"):
        decode_page(body, "income", {"600519.SH"}, {"2025-12-31"})


@pytest.mark.parametrize(
    "reference",
    [[], None, {"schema_version": 1, "records": None}, {"schema_version": 1, "records": [1]}],
)
def test_reference_input_types_are_explicit_errors(reference):
    """独立参考文件同样校验结构，不能把输入失误变成未捕获异常。"""
    with pytest.raises(ValueError):
        compare_reference([], reference)


def test_online_failure_summary_keeps_full_evidence_and_recovery_command(tmp_path, capsys):
    """短控制台输出仍保留完整异常、原失败捕获和准确恢复参数。"""
    workspace = tmp_path / "workspace"
    detail = "人工上游错误" * 200

    def failed_capture(codes, periods, directory, network, progress):
        directory.mkdir(parents=True)
        manifest = directory / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "eastmoney",
                    "capture_kind": "real",
                    "codes": list(codes),
                    "periods": list(periods),
                    "pages": [],
                    "complete": False,
                    "captured_at": utc_now(),
                    "errors": [{"status": "request_error", "error": detail}],
                }
            ),
            encoding="utf-8",
        )
        return manifest

    with patch("finpipe.ingestion.collect", side_effect=failed_capture):
        assert main(["run", "--sample-size", "1", "--workspace", str(workspace)]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["error_truncated"] is True and len(result["error"]) <= 300
    assert result["run_id"] in result["recovery_command_powershell"]
    assert "resume" in result["recovery_command_powershell"]
    with Store(workspace, readonly=True) as store:
        full = json.loads(
            store.conn.execute(
                "SELECT error_json FROM runs WHERE run_id=?", (result["run_id"],)
            ).fetchone()[0]
        )["message"]
    assert detail in full
    assert result["manifest_path"] and result["capture_dir"]
