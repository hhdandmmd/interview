"""发布前验证精确成员和血缘，防止完整目录掩盖错误的业务快照。"""

from __future__ import annotations

import json

from finpipe.exports import export_candidate, publication_receipt, sha256, validate_files
from finpipe.features import FEATURE_NAMES
from finpipe.identity import canonical_json
from finpipe.ingestion import load_manifest
from finpipe.models import BusinessKey
from finpipe.monitoring import _alert_id, _recover_scope
from finpipe.runtime import fault
from finpipe.source_records import _visible_state, plan_manifest
from finpipe.storage.database import Store, key_text
from finpipe.timeutil import utc_now


def validate_snapshot(store: Store, snapshot_id: str):
    """验证成员业务唯一性、处理口径和每条精确血缘，恢复也必须执行。"""
    snapshot = store.snapshot(snapshot_id)
    profile = store.profile(snapshot["profile_id"])
    records = {}
    for row in store.conn.execute(
        "SELECT * FROM snapshot_records WHERE snapshot_id=?", (snapshot_id,)
    ):
        state_row = store.conn.execute(
            "SELECT payload_json FROM record_states WHERE record_state_id=?",
            (row["record_state_id"],),
        ).fetchone()
        state = json.loads(state_row[0])
        if (
            canonical_json(state["key"]) != row["business_key"]
            or row["data_available_at"] > snapshot["data_as_of"]
        ):
            raise ValueError("记录成员业务键或时间边界不一致")
        clean_id = row["selected_clean_version_id"]
        if state["state_status"] == "selected":
            clean = store.conn.execute(
                "SELECT * FROM clean_versions WHERE clean_version_id=?", (clean_id,)
            ).fetchone()
            if (
                not clean
                or clean["source_version_id"] != state["selected_source_version_id"]
                or clean["mapping_version"] != profile["mapping_version"]
                or clean["cleaning_rule_version"] != profile["cleaning_rule_version"]
            ):
                raise ValueError("选中清洗版与来源/profile不一致")
            source = store.source(clean["source_version_id"])
            if key_text(source.key) != row["business_key"]:
                raise ValueError("清洗来源属于另一业务键")
        elif clean_id is not None:
            raise ValueError("冲突/缺失状态不能偷偷选旧clean")
        records[row["business_key"]] = row
    features = store.features(snapshot_id)
    scope = json.loads(store.scope(snapshot["scope_id"])["content_json"])
    expected_targets = {
        (code, period, name)
        for code in scope["codes"]
        for period in scope["output_periods"]
        for name in FEATURE_NAMES
    }
    actual_targets = {
        (feature.security_code, feature.report_end, feature.feature_name) for feature in features
    }
    expected_records = {
        key_text(BusinessKey("eastmoney", code, period, table))
        for code in scope["codes"]
        for period in scope["input_periods"]
        for table in ("income", "balance", "indicator")
    }
    if (
        set(records) != expected_records
        or len(features) != len(expected_targets)
        or actual_targets != expected_targets
    ):
        raise ValueError("快照成员缺少完整业务网格")
    for feature in features:
        identity = store.conn.execute(
            "SELECT scope_id,profile_id,security_code,report_end,feature_name FROM feature_versions WHERE feature_version_id=?",
            (feature.feature_version_id,),
        ).fetchone()
        if (
            identity["scope_id"] != snapshot["scope_id"]
            or identity["profile_id"] != snapshot["profile_id"]
            or tuple(identity)[2:]
            != (feature.security_code, feature.report_end, feature.feature_name)
        ):
            raise ValueError("因子版本与成员的范围、口径或目标不一致")
        if feature.data_available_at > snapshot["data_as_of"]:
            raise ValueError("因子输入尚未可用")
        if not feature.inputs or feature.data_available_at != max(
            entry.input_available_at for entry in feature.inputs
        ):
            raise ValueError("因子可用时间没有准确输入支撑")
        for entry in feature.inputs:
            member = records.get(key_text(entry.key))
            if (
                not member
                or member["record_state_id"] != entry.record_state_id
                or member["selected_clean_version_id"] != entry.clean_version_id
            ):
                raise ValueError("因子血缘不匹配精确快照成员")
            if (
                entry.input_available_at != member["data_available_at"]
                or entry.source_available_at != member["source_available_at"]
            ):
                raise ValueError("因子与记录冻结时间不一致")


def snapshot_sources_current(store: Store, snapshot_id: str) -> bool:
    """只检查成员之后的新状态；当前消费和激活不能跳过尚未发布的坏事实。"""
    snapshot = store.snapshot(snapshot_id)
    profile = store.profile(snapshot["profile_id"])
    changed = store.conn.execute(
        "SELECT m.business_key,m.record_state_id FROM snapshot_records m "
        "JOIN record_states original ON original.record_state_id=m.record_state_id "
        "WHERE m.snapshot_id=? AND EXISTS(SELECT 1 FROM record_states newer "
        "WHERE newer.business_key=m.business_key AND newer.state_sequence>original.state_sequence)",
        (snapshot_id,),
    )
    cutoff = utc_now()
    for row in changed:
        latest = store.latest_state(BusinessKey(**json.loads(row["business_key"])))
        visible, _, _ = _visible_state(store, latest, profile, cutoff, persist_clean=False)
        if visible is None or visible.record_state_id != row["record_state_id"]:
            return False
    return True


def publish_candidate(store: Store, snapshot_id: str, quality: dict | None = None) -> dict:
    """完成seal、同盘rename及条件激活；SQL已发布只修回执。"""
    snapshot = store.snapshot(snapshot_id)
    run_id = snapshot["run_id"]
    if snapshot["status"] == "PUBLISHED":
        from finpipe.ops import validate_published

        validate_published(store, snapshot_id)
        publication_receipt(store, snapshot_id)
        return _published_result(store, snapshot_id)
    validate_snapshot(store, snapshot_id)
    target = store.workspace / "snapshots" / snapshot_id
    staging = store.workspace / "staging" / snapshot_id
    if target.exists():
        directory = target
    elif (staging / "manifest.json").exists():
        directory = staging
    else:
        if quality is None:
            raise ValueError("未封存候选缺质量报告，须按原计划恢复处理")
        directory = export_candidate(store, snapshot_id, quality)
    manifest = validate_files(directory, snapshot_id)
    for name in ("scope_id", "profile_id", "universe_id", "expected_parent_head", "data_as_of"):
        if manifest.get(name) != snapshot[name]:
            raise ValueError("文件清单与固定候选元数据不一致")
    sealed_hash = sha256(directory / "manifest.json")
    if snapshot["manifest_hash"] and snapshot["manifest_hash"] != sealed_hash:
        raise ValueError("候选封存摘要改变，恢复拒绝重新采信被修改的清单")
    if not snapshot["manifest_hash"]:
        with store.transaction():
            store.conn.execute(
                "UPDATE dataset_snapshots SET manifest_hash=?,exported_at=? WHERE snapshot_id=?",
                (sealed_hash, manifest["exported_at"], snapshot_id),
            )
    fault("after_seal")
    target.parent.mkdir(parents=True, exist_ok=True)
    if directory != target:
        directory.rename(target)
    fault("after_rename")
    if not store.activate(snapshot_id, sha256(target / "manifest.json"), manifest["exported_at"]):
        with store.transaction():
            store.conn.execute(
                "UPDATE runs SET status='SUPERSEDED',phase='DONE',finished_at=? WHERE run_id=?",
                (utc_now(), run_id),
            )
        return {
            "status": "SUPERSEDED",
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "scope_id": snapshot["scope_id"],
        }
    with store.transaction():
        store.conn.execute(
            "UPDATE runs SET status='SUCCEEDED',phase='DONE',finished_at=? WHERE run_id=?",
            (utc_now(), run_id),
        )
        active = {
            _alert_id(snapshot["scope_id"], event)
            for event in manifest["quality"]["events"]
            if event.get("severity") != "info"
        }
        run = store.conn.execute("SELECT plan_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        source_manifest = load_manifest(plan_manifest(store, json.loads(run[0])))
        _recover_scope(store, run_id, snapshot["scope_id"], source_manifest, active)
    fault("after_activate")
    publication_receipt(store, snapshot_id)
    return _published_result(store, snapshot_id)


def _published_result(store: Store, snapshot_id: str) -> dict:
    snapshot = store.snapshot(snapshot_id)
    run = store.conn.execute(
        "SELECT counts_json FROM runs WHERE run_id=?", (snapshot["run_id"],)
    ).fetchone()
    return {
        "status": "PUBLISHED",
        "run_id": snapshot["run_id"],
        "snapshot_id": snapshot_id,
        "scope_id": snapshot["scope_id"],
        "counts": json.loads(run[0]),
        "export_path": str(store.workspace / "snapshots" / snapshot_id),
    }
