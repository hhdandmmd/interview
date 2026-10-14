"""复审反例回归：验证口径、同刻版本和扫描恢复义务，全部使用隔离人工数据。"""

import copy
import tempfile
from pathlib import Path

import pytest
from fixtures.financial_cases import PERIODS, source_rows, write_manifest

from finpipe.config import load_config
from finpipe.models import BusinessKey
from finpipe.monitoring import _emit_alert, _recover_scope
from finpipe.pipeline import _new_run, _register
from finpipe.reconciliation import compare_reference
from finpipe.source_records import _decide, _source_record
from finpipe.storage import Store
from finpipe.transforms import normalize


def reference_pair():
    """人工金额为100元累计收入，证据地址明确为示意地址，不冒充真实原报告。"""
    key = {
        "security_code": "600519.SH",
        "report_end": "2024-12-31",
        "statement_type": "income",
        "field_name": "operating_revenue_ytd",
    }
    left = {
        **key,
        "value": 100.0,
        "unit": "cny",
        "period_basis": "ytd",
        "consolidation_scope": "consolidated",
        "field_status": "ok",
        "time_valid": True,
        "quality_status": "accepted",
    }
    right = {
        **key,
        "reference_value": 100.0,
        "normalized_unit": "cny",
        "period_basis": "ytd",
        "consolidation_scope": "consolidated",
        "reference_url": "https://example.invalid/人工财报",
        "document_sha256": "0" * 64,
        "pdf_page": 1,
        "report_version": "人工测试版本",
        "abs_tolerance": 0.01,
        "rel_tolerance": 0.0,
    }
    return left, right


@pytest.mark.parametrize(
    "patch",
    [
        {"quality_status": "quarantined"},
        {"time_valid": False},
        {"period_basis": None},
        {"period_basis": "quarter"},
        {"unit": "multiple"},
        {"consolidation_scope": "parent"},
    ],
)
def test_equal_numbers_do_not_hide_incompatible_records(patch):
    """数值相等不能替代币种、期间、范围及有效时间契约。"""
    left, right = reference_pair()
    result = compare_reference([{**left, **patch}], {"schema_version": 1, "records": [right]})
    assert result["counts"] == {"not_comparable": 1}
    assert result["match_rate"] is None


def test_both_missing_basis_is_not_comparable():
    """两侧都未声明口径不能因None等于None而计为核验成功。"""
    left, right = reference_pair()
    left.pop("period_basis")
    right.pop("period_basis")
    assert compare_reference([left], {"schema_version": 1, "records": [right]})["counts"] == {
        "not_comparable": 1
    }


def test_outer_join_and_nulls_keep_unverified_keys():
    """外连接保留丢键，两个NULL不算匹配；覆盖率分母包括全部原计划。"""
    left, right = reference_pair()
    result = compare_reference(
        [left, {**left, "security_code": "000333.SZ"}],
        {"schema_version": 1, "records": [right, {**right, "security_code": "000651.SZ"}]},
    )
    assert result["counts"] == {"matched": 1, "left_only": 1, "right_only": 1}
    assert result["comparison_coverage"] == result["reference_coverage"] == 0.5
    result = compare_reference(
        [{**left, "value": None}],
        {"schema_version": 1, "records": [{**right, "reference_value": None}]},
    )
    assert result["counts"] == {"both_missing": 1}
    assert result["match_rate"] is None


@pytest.mark.parametrize("value,expected", [(100.01, "matched"), (100.010001, "mismatch")])
def test_disclosed_precision_boundary(value, expected):
    """金额精度以十进制容差判断，恰在边界与略超边界分别核验。"""
    left, right = reference_pair()
    assert compare_reference([{**left, "value": value}], {"schema_version": 1, "records": [right]})[
        "counts"
    ] == {expected: 1}


def test_currency_assumption_does_not_overwrite_existing_evidence():
    """真实来源入口不能用默认CNY覆盖明确的人工USD证据。"""
    raw = copy.deepcopy(source_rows("income")[0])
    raw["CURRENCY"] = None
    raw["_evidence"]["currency_assumed"] = "USD"
    source = _source_record(raw, "income", "2025-05-01T08:00:00.000000Z")
    assert source.raw["_evidence"]["currency_assumed"] == "USD"
    assert (
        normalize(source, load_config()["profile"], "2025-05-02T08:00:00.000000Z").quality_status
        == "quarantined"
    )


def test_simultaneous_semantic_conflict_is_independent_of_row_order():
    """同刻同金额但币种不同必须冲突，不靠HTTP行顺序决定正常或隔离。"""
    raw = copy.deepcopy(source_rows("income")[0])
    raw.pop("_revision_sequence", None)
    raw["_evidence"].pop("authoritative_revision", None)
    a = _source_record({**raw, "CURRENCY": "CNY"}, "income", "2025-05-01T08:00:00.000000Z")
    b = _source_record({**raw, "CURRENCY": "USD"}, "income", "2025-05-01T08:00:00.000000Z")
    for candidates in ([a, b], [b, a]):
        with (
            tempfile.TemporaryDirectory(prefix="finpipe 人工冲突 ") as folder,
            Store(Path(folder)) as store,
        ):
            with store.transaction():
                for source in candidates:
                    store.save_source(source)
                state = _decide(
                    store,
                    BusinessKey(**a.key.__dict__),
                    candidates,
                    "2025-05-02T08:00:00.000000Z",
                    "synthetic",
                    {source.source_version_id: source.first_seen_at for source in candidates},
                )
            assert state.state_status == "conflict"
            assert state.selected_source_version_id is None


def test_partial_new_scan_cannot_erase_an_older_source_failure():
    """质量阻断覆盖scope原因后，源失败仍须按自身范围和时区证据恢复。"""
    with tempfile.TemporaryDirectory(prefix="finpipe 人工告警 ") as folder:
        workspace = Path(folder)
        manifest_path = write_manifest(workspace / "fixture")
        from finpipe.ingestion import load_manifest

        manifest = load_manifest(manifest_path)
        config = load_config(workspace=workspace)
        with Store(workspace) as store:
            scope_id, _, scope = _register(store, manifest, config)
            run_id = _new_run(store, scope_id, config, manifest_path)
            with store.transaction():
                _emit_alert(
                    store,
                    scope_id,
                    run_id,
                    {
                        "rule_id": "upstream_capture_failed",
                        "requires_new_scan": True,
                        "failed_at": "2025-05-02T08:00:00.000000Z",
                        "failed_scan": {"codes": scope["codes"], "periods": list(PERIODS)},
                    },
                )
                store.conn.execute(
                    "UPDATE scopes SET health_status='blocked',health_reason='[\"quality_failure\"]' WHERE scope_id=?",
                    (scope_id,),
                )
                partial = {
                    **manifest,
                    "captured_at": "2025-05-02T17:00:00+08:00",
                    "periods": list(PERIODS[4:]),
                }
                _recover_scope(store, run_id, scope_id, partial, set())
            assert store.scope(scope_id)["health_status"] == "blocked"
            assert store.conn.execute("SELECT status FROM alerts").fetchone()[0] == "open"
            with store.transaction():
                _recover_scope(
                    store,
                    run_id,
                    scope_id,
                    {**manifest, "captured_at": "2025-05-02T15:59:00+08:00"},
                    set(),
                )
            assert store.scope(scope_id)["health_status"] == "blocked"
            with store.transaction():
                _recover_scope(
                    store,
                    run_id,
                    scope_id,
                    {**manifest, "captured_at": "2025-05-02T16:01:00+08:00"},
                    set(),
                )
            assert store.scope(scope_id)["health_status"] == "healthy"
            assert store.conn.execute("SELECT status FROM alerts").fetchone()[0] == "resolved"
