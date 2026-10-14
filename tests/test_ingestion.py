"""独立验证接入契约、冷却边界和不可变原文，不访问真实网络。"""

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from finpipe.catalog import FIELDS, REPORT_NAMES
from finpipe.ingestion import ManifestError, collect, load_manifest, select_universe
from finpipe.ingestion.archive import archive_body, decode_page, save_manifest
from finpipe.ingestion.client import SerialClient, period_groups, retry_after
from finpipe.ingestion.universe import choose_companies, is_financial


def row(kind, period="2024-12-31", code="600519.SH"):
    """使用合成金额，只验证字段及业务身份，不冒充真实财报。"""
    return {
        "SECUCODE": code,
        "REPORT_DATE": period,
        "NOTICE_DATE": "2025-04-01",
        **{spec.source_field: 100.0 for spec in FIELDS[kind]},
    }


def response(rows, count=None, pages=1, status=200, headers=None):
    """模拟原始响应字节，包含标准分页元数据。"""
    payload = {
        "success": True,
        "result": {"data": rows, "count": len(rows) if count is None else count, "pages": pages},
    }
    fake = Mock(status_code=status, headers=headers or {})
    fake.content = json.dumps(payload).encode()
    return fake


def fixture_manifest(directory: Path):
    """三表各一页的完整合成清单，声明夹具身份。"""
    pages = []
    for kind, name in REPORT_NAMES.items():
        relative, digest = archive_body(directory, response([row(kind)]).content)
        pages.append(
            {
                "page_id": kind,
                "body_path": relative,
                "raw_hash": digest,
                "report_type": kind,
                "report_name": name,
                "observed_at": "2026-10-13T00:00:00Z",
                "request": {"params": {"reportName": name, "pageNumber": "1", "pageSize": "1"}},
                "rows": [{"forged": "缓存不可信"}],
            }
        )
    return save_manifest(
        directory,
        {
            "schema_version": 1,
            "source": "eastmoney",
            "codes": ["600519.SH"],
            "periods": ["2024-12-31"],
            "capture_kind": "synthetic",
            "captured_at": "2026-10-13T00:00:00Z",
            "complete": True,
            "errors": [],
            "pages": pages,
        },
    )


def test_manifest_reparses_raw_and_keeps_observation(tmp_path):
    loaded = load_manifest(fixture_manifest(tmp_path))
    assert loaded["capture_kind"] == "synthetic"
    assert loaded["pages"][0]["rows"] == [row("income")]
    assert loaded["pages"][0]["observed_at"] == "2026-10-13T00:00:00Z"


def test_raw_tampering_rejected(tmp_path):
    path = fixture_manifest(tmp_path)
    page = json.loads(path.read_text(encoding="utf-8"))["pages"][0]
    (tmp_path / page["body_path"]).write_bytes(b"{}")
    with pytest.raises(ManifestError, match="SHA256"):
        load_manifest(path)


def test_directory_escape_rejected(tmp_path):
    path = fixture_manifest(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["pages"][0]["body_path"] = "../outside.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ManifestError, match="目录内"):
        load_manifest(path)


def test_raw_archive_is_content_addressed_and_never_overwritten(tmp_path):
    first = archive_body(tmp_path, b"{}")
    second = archive_body(tmp_path, b"{}")
    assert first == second
    assert first[1] == hashlib.sha256(b"{}").hexdigest()
    (tmp_path / first[0]).write_bytes(b"tampered")
    with pytest.raises(ManifestError):
        archive_body(tmp_path, b"{}")


def test_collect_paginates_and_validates_all_three_reports(tmp_path):
    periods = ("2024-09-30", "2024-12-31")
    replies = [
        response([row(kind, p)], count=2, pages=2)
        for kind in REPORT_NAMES
        for p in reversed(periods)
    ]
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep") as sleep,
    ):
        session.return_value.get.side_effect = replies
        path = collect(("600519.SH",), periods, tmp_path, {"page_size": 1})
        assert session.return_value.get.call_count == 6
        assert all(call.args[0] >= 1 for call in sleep.call_args_list)
    loaded = load_manifest(path)
    assert loaded["complete"] is True
    assert len(loaded["pages"]) == 6


def test_complete_capture_keeps_missing_keys_for_business_coverage_gate(tmp_path):
    replies = [response([row(kind)]) for kind in REPORT_NAMES]
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.side_effect = replies
        path = collect(("600519.SH",), ("2024-09-30", "2024-12-31"), tmp_path, {})
    loaded = load_manifest(path)
    assert loaded["complete"] is True
    assert len(loaded["missing_keys"]) == 3
    assert all(item["report_end"] == "2024-09-30" for item in loaded["missing_keys"])


def test_claiming_complete_without_scanning_one_statement_is_rejected(tmp_path):
    path = fixture_manifest(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["pages"].pop()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ManifestError, match="扫描"):
        load_manifest(path)


def test_403_is_not_retried_or_bypassed_by_other_reports(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.return_value = response([], status=403)
        path = collect(("600519.SH",), ("2024-12-31",), tmp_path, {})
        assert session.return_value.get.call_count == 1
    manifest = load_manifest(path)
    assert not manifest["complete"]
    assert any(error["status"] == "not_attempted" for error in manifest["errors"])
    assert any(error.get("body_path") for error in manifest["errors"])


def test_long_retry_after_blocks_whole_source(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep") as sleep,
    ):
        session.return_value.get.return_value = response(
            [], status=429, headers={"Retry-After": "60"}
        )
        path = collect(("600519.SH", "000333.SZ"), ("2024-12-31",), tmp_path, {})
        assert session.return_value.get.call_count == 1
        assert sleep.call_count == 1
    manifest = load_manifest(path)
    assert any(error["status"] == "retry_deferred" for error in manifest["errors"])
    assert sum(error["status"] == "not_attempted" for error in manifest["errors"]) == 5


def test_short_retry_preserves_every_attempt_and_wait(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep") as sleep,
    ):
        session.return_value.get.side_effect = [
            response([], status=503, headers={"Retry-After": "4"}),
            response([row("income")]),
        ]
        client = SerialClient({}, tmp_path)
        body, evidence = client.get({"reportName": REPORT_NAMES["income"]})
        client.close()
        assert body is not None
        assert [call.args[0] for call in sleep.call_args_list] == [1.0, 4.0]
        assert len(evidence["attempts"]) == 2
        assert all((tmp_path / attempt["body_path"]).exists() for attempt in evidence["attempts"])


def test_network_retry_is_finite(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.side_effect = requests.exceptions.Timeout("合成超时")
        client = SerialClient({}, tmp_path)
        body, evidence = client.get({})
        client.close()
        assert body is None
        assert len(evidence["attempts"]) == 3
        assert session.return_value.get.call_count == 3


def test_tls_error_is_not_retried(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.side_effect = requests.exceptions.SSLError("合成证书错误")
        client = SerialClient({}, tmp_path)
        body, evidence = client.get({})
        client.close()
        assert body is None and evidence["status"] == "request_error"
        assert session.return_value.get.call_count == 1


def test_paging_drift_prevents_complete_publication(tmp_path):
    replies = [
        response([row("income", "2024-12-31")], count=2, pages=2),
        response([row("income", "2024-09-30")], count=3, pages=2),
        response([row("balance", "2024-12-31"), row("balance", "2024-09-30")]),
        response([row("indicator", "2024-12-31"), row("indicator", "2024-09-30")]),
    ]
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.side_effect = replies
        path = collect(("600519.SH",), ("2024-09-30", "2024-12-31"), tmp_path, {"page_size": 1})
    assert not load_manifest(path)["complete"]


def test_missing_field_invalidates_page_but_keeps_body(tmp_path):
    malformed = row("income")
    del malformed["OPERATE_INCOME"]
    replies = [response([malformed]), response([row("balance")]), response([row("indicator")])]
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.side_effect = replies
        path = collect(("600519.SH",), ("2024-12-31",), tmp_path, {})
    manifest = load_manifest(path)
    assert any(
        error["status"] == "schema_error" and error.get("body_path") for error in manifest["errors"]
    )


def test_sparse_quarters_are_separate_requests():
    assert period_groups(("2024-03-31", "2024-09-30", "2024-12-31")) == [
        ("2024-03-31",),
        ("2024-09-30", "2024-12-31"),
    ]


def test_http_date_retry_after():
    assert (
        retry_after("Tue, 13 Oct 2026 00:00:04 GMT", datetime(2026, 10, 13, tzinfo=timezone.utc))
        == 4.0
    )
    assert retry_after("错误日期", datetime.now(timezone.utc)) is None


def test_existing_manifest_is_never_overwritten(tmp_path):
    path = fixture_manifest(tmp_path)
    frozen = path.read_bytes()
    with pytest.raises(FileExistsError):
        collect(("600519.SH",), ("2024-12-31",), tmp_path, {})
    assert path.read_bytes() == frozen


def test_financial_industry_classification_uses_source_category():
    assert is_financial("银行Ⅱ")
    assert is_financial("证券II")
    assert is_financial("多元金融")
    assert not is_financial("一般零售")
    assert not is_financial("软件开发")


def test_universe_small_sample_has_three_markets():
    companies = [
        {"security_code": code, "exchange": code[-2:]}
        for code in ("600000.SH", "000002.SZ", "920001.BJ", "600001.SH")
    ]
    assert {item["exchange"] for item in choose_companies(companies, 3)} == {"SH", "SZ", "BJ"}


def test_universe_excludes_financial_and_unknown_preserves_evidence(tmp_path):
    codes = ("600000.SH", "000002.SZ", "920001.BJ", "000001.SZ", "600001.SH")
    industries = ("一般零售", "房地产开发", "机械设备", "银行Ⅱ", None)
    rows = [
        {
            "SECUCODE": code,
            "SECURITY_NAME_ABBR": "合成公司",
            "REPORTDATE": "2025-12-31",
            "SECURITY_TYPE_CODE": "058001001",
            "BOARD_NAME": industry,
            "BOARD_CODE": "BK0000",
        }
        for code, industry in zip(codes, industries)
    ]
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.return_value = response(rows)
        document = select_universe(3, {}, tmp_path)
    assert document["complete"]
    assert len(document["codes"]) == 3
    assert document["excluded_financial"] == 1
    assert document["unknown_industry"] == 1
    assert "000001.SZ" not in document["codes"]
    assert all(item["historical_eligibility"] == "unknown" for item in document["companies"])
    assert all(item["qualification_evidence"]["raw_hash"] for item in document["companies"])


def test_universe_insufficient_verified_companies_does_not_guess(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.return_value = response([], count=0, pages=0)
        document = select_universe(50, {}, tmp_path)
    assert not document["complete"]
    assert document["codes"] == []
    assert document["errors"]


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e309"])
def test_nonfinite_json_cannot_destroy_manifest_serialization(number):
    body = ('{"success":true,"result":{"count":1,"pages":1,"data":[' + number + "]}}").encode()
    with pytest.raises(ManifestError):
        decode_page(body, "income", {"600519.SH"}, {"2024-12-31"})


def test_semantic_bad_amount_is_preserved_for_cleaning_decision():
    malformed = row("income")
    malformed["OPERATE_INCOME"] = "非法金额"
    malformed["NETPROFIT"] = None
    body = response([malformed]).content
    assert decode_page(body, "income", {"600519.SH"}, {"2024-12-31"})["rows"] == [malformed]


def test_report_date_with_valid_prefix_and_invalid_suffix_is_rejected():
    malformed = row("income")
    malformed["REPORT_DATE"] = "2024-12-31 不是时间"
    with pytest.raises(ManifestError, match="报告日期"):
        decode_page(response([malformed]).content, "income", {"600519.SH"}, {"2024-12-31"})


def test_checkpoint_survives_real_process_exit_before_final_manifest(tmp_path):
    """子进程直接退出，不执行 finally；已收到页的真实上下文仍可追溯。"""
    script = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        from unittest.mock import Mock
        from finpipe.catalog import FIELDS
        from finpipe.ingestion import collect
        import finpipe.ingestion.client as client
        row = {'SECUCODE':'600519.SH','REPORT_DATE':'2024-12-31','NOTICE_DATE':'2025-04-01',
               **{field.source_field:100.0 for field in FIELDS['income']}}
        response = Mock(status_code=200, headers={})
        response.content = json.dumps({'success':True,'result':{'count':1,'pages':1,'data':[row]}}).encode()
        session = Mock()
        session.get.return_value = response
        client.requests.Session = lambda: session
        client.time.sleep = lambda _: None
        def progress(event):
            if event.get('status') == 'page_validated':
                os._exit(73)
        collect(('600519.SH',),('2024-12-31',),Path(sys.argv[1]),{},progress)
    """)
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 73
    assert not (tmp_path / "manifest.json").exists()
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["phase"] == "COLLECTING"
    assert checkpoint["complete"] is False
    assert len(checkpoint["pages"]) == 1
    assert "rows" not in checkpoint["pages"][0]
    assert checkpoint["current_request"]["attempt"] == 1
    assert checkpoint["current_request"]["observed_at"]
    page = checkpoint["pages"][0]
    assert (
        hashlib.sha256((tmp_path / page["body_path"]).read_bytes()).hexdigest() == page["raw_hash"]
    )
    with pytest.raises(FileExistsError, match="跨会话"):
        collect(("600519.SH",), ("2024-12-31",), tmp_path, {})


def test_checkpoint_records_completed_groups_without_impersonating_manifest(tmp_path):
    replies = [response([row(kind)]) for kind in REPORT_NAMES]
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.side_effect = replies
        collect(("600519.SH",), ("2024-12-31",), tmp_path, {})
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["capture_complete"] is True
    assert checkpoint["complete"] is False
    assert len(checkpoint["completed_groups"]) == 3
    assert all(group["status"] == "completed" for group in checkpoint["request_groups"])
    assert not (tmp_path / "checkpoint.json.tmp").exists()


def test_checkpoint_preserves_retry_after_during_interrupted_short_wait(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep") as sleep,
    ):
        session.return_value.get.return_value = response(
            [], status=429, headers={"Retry-After": "4"}
        )
        sleep.side_effect = [None, KeyboardInterrupt("合成等待中断")]
        with pytest.raises(KeyboardInterrupt):
            collect(("600519.SH",), ("2024-12-31",), tmp_path, {})
        assert session.return_value.get.call_count == 1
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["phase"] == "INTERRUPTED"
    assert checkpoint["source_blocked"]["status"] == "retry_wait"
    assert checkpoint["source_blocked"]["retry_after"] == "4"
    assert checkpoint["source_blocked"]["next_retry_at"]
    assert len(checkpoint["attempts"]) == 1
    assert checkpoint["attempts"][0]["body_path"]


def test_last_429_has_persisted_retry_time_for_next_session(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.return_value = response(
            [], status=429, headers={"Retry-After": "2"}
        )
        collect(("600519.SH",), ("2024-12-31",), tmp_path, {"max_attempts": 1})
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["source_blocked"]["status"] == "retry_exhausted"
    assert checkpoint["source_blocked"]["next_retry_at"]
    assert checkpoint["current_request"]["attempt"] == 1
    assert checkpoint["current_request"]["http_status"] == 429


def test_403_checkpoint_requires_manual_review_and_keeps_actual_request(tmp_path):
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.return_value = response([], status=403)
        collect(("600519.SH",), ("2024-12-31",), tmp_path, {})
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["source_blocked"]["manual_review_required"] is True
    assert checkpoint["source_blocked"]["status"] == "http_403"
    assert checkpoint["current_request"]["http_status"] == 403
    assert checkpoint["last_skipped_request"]["status"] == "not_attempted"


@pytest.mark.parametrize(
    "exception",
    [requests.exceptions.SSLError("合成证书错误"), requests.exceptions.InvalidURL("合成请求错误")],
)
def test_permanent_request_errors_keep_attempt_start_and_observation(tmp_path, exception):
    events = []
    with (
        patch("finpipe.ingestion.client.requests.Session") as session,
        patch("finpipe.ingestion.client.time.sleep"),
    ):
        session.return_value.get.side_effect = exception
        client = SerialClient({}, tmp_path, events.append)
        body, evidence = client.get({"reportName": REPORT_NAMES["income"]})
        client.close()
    assert body is None and evidence["attempt"] == 1
    assert evidence["started_at"] <= evidence["observed_at"]
    assert len(evidence["attempts"]) == 1
    assert [event["status"] for event in events] == ["requesting", "request_error"]
