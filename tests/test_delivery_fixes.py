"""独立人工反例验证交付整改，不修改真实归档或已封存快照。"""

import copy
import json
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fixtures.financial_cases import source_rows, write_manifest
from test_finance import calculate, fixture

from finpipe.config import load_config
from finpipe.identity import canonical_json, stable_id
from finpipe.ops import health, ingest_diagnostics
from finpipe.pipeline import _new_run, execute_manifest
from finpipe.source_records import _decide, _source_record
from finpipe.storage import Store
from finpipe.transforms import normalize


def test_real_branch_selects_current_complete_observation_and_preserves_history(tmp_path):
    """人工探针走真实来源分支：更正、重复、旧重放和A→B→A均有明确结果。"""
    raw = copy.deepcopy(source_rows("income")[0])
    raw["OPERATE_INCOME"] = 100
    a = _source_record(raw, "income", "2025-05-01T08:00:00.000000Z")
    b = _source_record({**raw, "OPERATE_INCOME": 120}, "income", "2025-05-02T08:00:00.000000Z")
    with Store(tmp_path) as store:

        def decide(records, observed):
            with store.transaction():
                for record in records:
                    store.save_source(record)
                state = _decide(
                    store,
                    a.key,
                    records,
                    observed,
                    "real",
                    {record.source_version_id: observed for record in records},
                )
                store.save_state(state)
            return state

        first = decide([a], a.first_seen_at)
        corrected = decide([b], b.first_seen_at)
        repeated = decide([b], "2025-05-03T08:00:00.000000Z")
        older = decide([a], a.first_seen_at)
        restored = decide([a], "2025-05-04T08:00:00.000000Z")
        assert first.selected_source_version_id == a.source_version_id
        assert corrected.state_status == "selected"
        assert corrected.selected_source_version_id == b.source_version_id
        assert repeated.record_state_id == corrected.record_state_id == older.record_state_id
        assert restored.selected_source_version_id == a.source_version_id
        assert restored.record_state_id != first.record_state_id
        assert store.conn.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0] == 2
        assert store.conn.execute("SELECT COUNT(*) FROM record_states").fetchone()[0] == 3
        assert not corrected.evidence["source_revision_verified"]


def test_real_branch_conflict_recovers_only_after_an_unambiguous_new_scan(tmp_path):
    """同一扫描异值冲突；后续唯一坏新版仍选新版并失效，不复活旧好值。"""
    raw = copy.deepcopy(source_rows("income")[0])
    a = _source_record(raw, "income", "2025-05-01T08:00:00.000000Z")
    b = _source_record({**raw, "OPERATE_INCOME": None}, "income", "2025-05-02T08:00:00.000000Z")
    with Store(tmp_path) as store, store.transaction():
        for record in (a, b):
            store.save_source(record)
        conflict = _decide(
            store,
            a.key,
            [b, a],
            b.first_seen_at,
            "real",
            {a.source_version_id: a.first_seen_at, b.source_version_id: b.first_seen_at},
        )
        store.save_state(conflict)
        assert conflict.state_status == "conflict"
        recovered = _decide(
            store,
            a.key,
            [b],
            "2025-05-03T08:00:00.000000Z",
            "real",
            {b.source_version_id: "2025-05-03T08:00:00.000000Z"},
        )
        assert recovered.state_status == "selected"
        clean = normalize(b, load_config()["profile"], recovered.decision_observed_at)
        assert clean.values["operating_revenue_ytd"] is None
        assert clean.field_states["operating_revenue_ytd"] == "missing"


@pytest.mark.parametrize(
    "basis,reason",
    [
        ("incompatible", "incompatible_basis"),
        ("known_risk_unresolved", "known_restatement_risk"),
    ],
)
@pytest.mark.parametrize("bad_period", ["2019-03-31", "2020-03-31"])
def test_q1_growth_and_roe_compare_both_period_bases(basis, reason, bad_period):
    """同比两端均拒绝明确不可比；本期单独披露的ROE和同期利润率仍可用。"""
    records, states = fixture()
    records = [
        replace(record, basis_status=basis)
        if record.key.report_end == bad_period
        and record.key.statement_type in ("income", "indicator")
        else record
        for record in records
    ]
    result = calculate(records, states, period="2020-03-31")
    for name in (
        "roe_change_yoy",
        "roe_change_yoy_pp",
        "revenue_growth_yoy",
        "parent_profit_growth_yoy",
    ):
        assert result[name].feature_value is None
        assert result[name].feature_status == "input_error"
        assert reason in result[name].reason_codes
    assert result["roe_weighted_ytd"].feature_value == pytest.approx(0.06)
    assert result["net_margin_q"].feature_value == pytest.approx(0.15)


def test_formula_version_is_explicit_and_old_frozen_formula_remains_reproducible():
    """修复进入公式2；显式旧公式只重现历史行为，不混入新默认profile。"""
    records, states = fixture()
    records = [
        replace(record, basis_status="incompatible")
        if record.key.report_end == "2020-03-31"
        else record
        for record in records
    ]
    old = calculate(records, states, period="2020-03-31", formula_version="1")
    new = calculate(records, states, period="2020-03-31", formula_version="2")
    assert old["roe_change_yoy"].feature_value == pytest.approx(0.2)
    assert new["roe_change_yoy"].feature_value is None
    assert load_config()["profile"]["formula_version"] == "2"


def published(workspace):
    """创建明确人工身份的独立工作区，供任务和告警反例共用。"""
    manifest = write_manifest(workspace.parent / "fixture")
    config = load_config(workspace=workspace)
    result = execute_manifest(manifest, config, allow_synthetic=True)
    assert result["status"] == "PUBLISHED"
    return result, config, manifest


def test_offline_success_does_not_hide_an_online_heartbeat_timeout(tmp_path):
    """在线超时按scope冻结任务检查，不被较新的离线成功记录覆盖。"""
    workspace = tmp_path / "workspace"
    result, config, manifest = published(workspace)
    old = (
        (datetime.now(timezone.utc) - timedelta(hours=3))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with Store(workspace) as store:
        online = _new_run(store, result["scope_id"], config, manifest)
        plan = json.loads(
            store.conn.execute("SELECT plan_json FROM runs WHERE run_id=?", (online,)).fetchone()[0]
        )
        plan["collection"] = {
            "codes": config["scope"]["codes"],
            "periods": list(config["input_periods"]),
        }
        with store.transaction():
            store.conn.execute(
                "UPDATE runs SET started_at=?,heartbeat_at=?,phase='COLLECTING',plan_json=? WHERE run_id=?",
                (old, old, canonical_json(plan), online),
            )
        offline = _new_run(store, result["scope_id"], config, manifest)
        with store.transaction():
            store.conn.execute(
                "UPDATE runs SET status='SUCCEEDED',phase='DONE' WHERE run_id=?", (offline,)
            )
    diagnostic = health(workspace)
    assert diagnostic["status"] == "unhealthy"
    assert "run_timeout" in diagnostic["rules"]
    assert diagnostic["scopes"][0]["scheduled_run_id"] == online


def test_health_recurrence_reopens_and_keeps_each_diagnostic_fact(tmp_path):
    """故障→恢复→复发重新打开；持续故障保留确认，旧文件不再次制造故障。"""
    workspace = tmp_path / "workspace"
    result, _, _ = published(workspace)
    artifact = workspace / "snapshots" / result["snapshot_id"] / "report.html"
    original = artifact.read_bytes()

    def ingest():
        with Store(workspace) as store, store.transaction():
            ingest_diagnostics(store)
            return store.conn.execute("SELECT * FROM alerts WHERE scope_id IS NULL").fetchone()

    artifact.write_bytes(original + b"temporary_test_corruption")
    first = health(workspace)
    assert ingest()["status"] == "open"
    with Store(workspace) as store, store.transaction():
        store.conn.execute("UPDATE alerts SET status='acknowledged' WHERE scope_id IS NULL")
    assert ingest()["status"] == "acknowledged"
    artifact.write_bytes(original)
    assert health(workspace)["status"] == "healthy"
    assert ingest()["status"] == "resolved"
    artifact.write_bytes(original + b"temporary_test_corruption")
    repeated = health(workspace)
    assert repeated["status"] == "unknown"
    assert repeated["event_id"] != first["event_id"]
    alert = ingest()
    assert alert["status"] == "open"
    assert alert["event_id"] == repeated["event_id"]
    assert (workspace / "diagnostics" / (first["event_id"] + ".json")).exists()
    artifact.write_bytes(original)
    health(workspace)
    assert ingest()["status"] == "resolved"
    assert ingest()["status"] == "resolved"


def execute_at(path, config, time):
    """冻结人工处理与发布时间，完整增量对照保留同一来源和裁决历史。"""
    with ExitStack() as stack:
        for module in (
            "pipeline",
            "monitoring",
            "publication",
            "exports",
            "ops",
            "storage.database",
        ):
            stack.enter_context(patch(f"finpipe.{module}.utc_now", return_value=time))
        return execute_manifest(path, config, allow_synthetic=True)


def test_incremental_and_full_results_equal_after_null_and_profile_change(tmp_path):
    """独立数据库强制全量，与公司级增量比较所有值、状态、时间和精确血缘。"""
    a = write_manifest(tmp_path / "a")
    configs = [load_config(workspace=tmp_path / kind) for kind in ("incremental", "full")]
    for config in configs:
        execute_at(a, config, "2025-05-03T08:00:00.000000Z")

    def change(table, rows):
        if table == "income":
            for row in rows:
                if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith("2024-06-30"):
                    row["OPERATE_COST"] = None
                    row["_revision_sequence"] = 2

    b = write_manifest(tmp_path / "b", change, "2025-05-02T08:00:00.000000Z")
    with Store(Path(configs[1]["workspace"])) as store, store.transaction():
        store.conn.execute("DELETE FROM workspace_head")
    runs = [execute_at(b, config, "2025-05-04T08:00:00.000000Z") for config in configs]
    assert runs[0]["counts"]["evaluated"] == 60
    assert runs[0]["counts"]["reused"] == 60
    assert runs[1]["counts"]["evaluated"] == 120

    def rows():
        output = []
        for config in configs:
            with Store(Path(config["workspace"]), readonly=True) as store:
                output.append(
                    [
                        asdict(feature)
                        for feature in store.features(store.select_snapshot()["snapshot_id"])
                    ]
                )
        return output

    assert rows()[0] == rows()[1]
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(
        "[profile.parameters]\namount_denominator=250\nratio_denominator=0.01\n", encoding="utf-8"
    )
    previous_profiles = [config["profile_id"] for config in configs]
    configs = [load_config(profile_path, Path(config["workspace"])) for config in configs]
    for index, config in enumerate(configs):
        assert config["profile_id"] != previous_profiles[index]
        upgraded = execute_at(b, config, "2025-05-05T08:00:00.000000Z")
        assert upgraded["counts"]["evaluated"] == 120
        assert upgraded["counts"]["source_new"] == 0
    assert rows()[0] == rows()[1]


def test_new_formula_profile_recomputes_even_when_source_did_not_change(tmp_path):
    """升级profile重算同来源，不能由旧head的相同来源签名短路为NO_CHANGE。"""
    path = write_manifest(tmp_path / "fixture")
    current = load_config(workspace=tmp_path / "workspace")
    old = copy.deepcopy(current)
    old["profile"]["formula_version"] = "1"
    old["profile"].pop("source_selection_policy")
    old["profile_id"] = stable_id("profile", old["profile"])
    first = execute_at(path, old, "2025-05-03T08:00:00.000000Z")
    upgraded = execute_at(path, current, "2025-05-04T08:00:00.000000Z")
    assert upgraded["status"] == "PUBLISHED"
    assert upgraded["counts"]["evaluated"] == 120
    assert upgraded["counts"]["source_new"] == 0
    assert first["snapshot_id"] != upgraded["snapshot_id"]
    with Store(Path(current["workspace"]), readonly=True) as store:
        assert store.snapshot(first["snapshot_id"])["profile_id"] == old["profile_id"]
        assert store.snapshot(upgraded["snapshot_id"])["profile_id"] == current["profile_id"]
        assert len(store.features(first["snapshot_id"])) == 120
