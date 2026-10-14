"""所有执行入口共享冻结计划、版本裁决、门禁和条件发布。"""

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from finpipe.catalog import FIELDS
from finpipe.features import compute_features, required_periods
from finpipe.identity import canonical_json, stable_id
from finpipe.ingestion import load_manifest
from finpipe.models import BusinessKey
from finpipe.monitoring import _alert_id, _emit_alert, _recover_scope, previous_quality
from finpipe.publication import publish_candidate
from finpipe.runtime import fault, workspace_lock
from finpipe.source_records import (
    _archive_manifest,
    _attempt_id,
    _decide,
    _save_attempts,
    _source_record,
    _visible_state,
    plan_manifest,
)
from finpipe.storage.database import Store, key_text
from finpipe.timeutil import iso_time, quarter_end, quarters, utc_now
from finpipe.transforms import normalize


def _scope(manifest: dict, config: dict) -> dict:
    periods = tuple(sorted(quarter_end(p) for p in manifest["periods"]))
    if len(periods) < 5 or tuple(quarters(periods[0], periods[-1])) != periods:
        raise ValueError("输入须为至少五个连续季度，含一年比较上下文")
    outputs = tuple(config["output_periods"]) if config.get("_output_explicit") else periods[4:]
    if not set(required_periods(outputs)) <= set(periods):
        raise ValueError("输入缺少公式所需准确季度上下文")
    return {
        "codes": sorted(manifest["codes"]),
        "input_periods": list(periods),
        "output_periods": list(outputs),
        "capture_kind": manifest["capture_kind"],
    }


def _register(store: Store, manifest: dict, config: dict) -> tuple[str, str, dict]:
    profile_id = config["profile_id"]
    scope = _scope(manifest, config)
    existing_kinds = {r[0] for r in store.conn.execute("SELECT DISTINCT capture_kind FROM scopes")}
    if existing_kinds and scope["capture_kind"] not in existing_kinds:
        raise ValueError("人工夹具和真实数据必须使用不同工作区")
    universe_id = stable_id("universe", scope["codes"])
    scope_id = stable_id(
        "synthetic_scope" if scope["capture_kind"] == "synthetic" else "scope", scope
    )
    with store.transaction():
        store.conn.execute(
            "INSERT OR IGNORE INTO processing_profiles VALUES(?,?)",
            (profile_id, canonical_json(config["profile"])),
        )
        store.conn.execute(
            "INSERT OR IGNORE INTO universe_snapshots VALUES(?,?)",
            (
                universe_id,
                canonical_json(
                    {
                        "codes": scope["codes"],
                        "selection": manifest.get("universe", {"kind": "explicit_manifest"}),
                    }
                ),
            ),
        )
        store.conn.execute(
            "INSERT OR IGNORE INTO scopes(scope_id,universe_id,capture_kind,content_json) VALUES(?,?,?,?)",
            (scope_id, universe_id, scope["capture_kind"], canonical_json(scope)),
        )
    return scope_id, universe_id, scope


def _new_run(
    store: Store, scope_id: str, config: dict, manifest_path: Path, parent_run_id: str | None = None
) -> str:
    run_id = "run_" + uuid.uuid4().hex
    now = utc_now()
    plan = {
        "config": config,
        "manifest_path": str(manifest_path.resolve()),
        "expected_parent_head": store.head(scope_id),
        "scope_id": scope_id,
        "scope": json.loads(store.scope(scope_id)["content_json"]),
    }
    with store.transaction():
        store.conn.execute(
            "INSERT INTO runs(run_id,scope_id,profile_id,parent_run_id,status,phase,started_at,heartbeat_at,plan_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                scope_id,
                config["profile_id"],
                parent_run_id,
                "RUNNING",
                "PROCESSING",
                now,
                now,
                canonical_json(plan),
            ),
        )
    return run_id


def _process(
    store: Store,
    run_id: str,
    manifest: dict,
    config: dict,
    scope_id: str,
    universe_id: str,
    scope: dict,
) -> dict:
    """按冻结输入完成来源裁决、增量计算、质量门禁及候选发布。"""
    from finpipe.quality import evaluate_quality

    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    plan = json.loads(run["plan_json"])
    if store.head(scope_id) != plan["expected_parent_head"]:
        with store.transaction():
            store.conn.execute(
                "UPDATE runs SET status='SUPERSEDED',phase='DONE',finished_at=? WHERE run_id=?",
                (utc_now(), run_id),
            )
        return {"status": "SUPERSEDED", "run_id": run_id, "scope_id": scope_id}
    profile = config["profile"]
    now = utc_now()
    grouped, incoming_times = {}, {}
    source_new = clean_new = state_new = 0
    with store.transaction():
        _save_attempts(store, run_id, manifest, Path(plan["manifest_path"]))
    if not manifest["complete"]:
        raise ValueError(
            "capture_incomplete：采集未完整扫描，保留旧裁决；"
            + canonical_json(manifest.get("errors", []))
        )
    with store.transaction():
        for page in manifest["pages"]:
            attempt_id = _attempt_id(page)
            for number, raw in enumerate(page["rows"]):
                record = _source_record(raw, page["report_type"], page["observed_at"])
                source_new += store.save_source(record)
                record = store.source(record.source_version_id)
                incoming_times[record.source_version_id] = max(
                    incoming_times.get(record.source_version_id, record.first_seen_at),
                    iso_time(page["observed_at"]),
                )
                observation = stable_id(
                    "observation",
                    {"attempt": attempt_id, "row": number, "source": record.source_version_id},
                )
                store.conn.execute(
                    "INSERT OR IGNORE INTO source_observations VALUES(?,?,?,?,?)",
                    (
                        observation,
                        record.source_version_id,
                        attempt_id,
                        number,
                        iso_time(page["observed_at"]),
                    ),
                )
                store.conn.execute(
                    "INSERT OR IGNORE INTO run_inputs VALUES(?,?)", (run_id, observation)
                )
                grouped.setdefault(record.key, []).append(record)
        states, records, expected, waiting = [], [], [], []
        known_expectations = {}
        for entry in store.conn.execute(
            "SELECT business_key,eligibility,evidence_json FROM expected_keys"
        ):
            captured = json.loads(entry["evidence_json"]).get("capture")
            previous = known_expectations.get(entry["business_key"])
            previous_capture = (
                json.loads(previous["evidence_json"]).get("capture") if previous else None
            )
            if (
                previous is None
                or captured
                and (not previous_capture or iso_time(captured) >= iso_time(previous_capture))
            ):
                known_expectations[entry["business_key"]] = entry
        for code in scope["codes"]:
            for period in scope["input_periods"]:
                for table in FIELDS:
                    key = BusinessKey("eastmoney", code, period, table)
                    candidates = grouped.get(key, [])
                    scanned = code in manifest["codes"] and period in manifest["periods"]
                    prior_expected = known_expectations.get(key_text(key))
                    if scanned and prior_expected:
                        prior_capture = json.loads(prior_expected["evidence_json"]).get("capture")
                        scanned = not prior_capture or iso_time(
                            manifest["captured_at"]
                        ) >= iso_time(prior_capture)
                    state = (
                        _decide(
                            store,
                            key,
                            candidates,
                            now,
                            scope["capture_kind"],
                            incoming_times,
                            profile.get("source_selection_policy", "legacy_conflict"),
                        )
                        if scanned
                        else store.latest_state(key)
                    )
                    if state is None:
                        raise ValueError("增量范围之外没有既有扫描事实，须先全量采集")
                    existed = store.conn.execute(
                        "SELECT 1 FROM record_states WHERE record_state_id=?",
                        (state.record_state_id,),
                    ).fetchone()
                    if not existed:
                        store.save_state(state)
                        state_new += 1
                    if state.selected_source_version_id:
                        source = store.source(state.selected_source_version_id)
                        if store.clean(source.source_version_id, profile) is None:
                            clean_new += store.save_clean(normalize(source, profile, now), profile)
                    visible, clean, ready_at = _visible_state(store, state, profile, now)
                    eligibility = (
                        ("eligible" if candidates else "unknown")
                        if scanned
                        else prior_expected["eligibility"]
                    )
                    qualification = (
                        {"capture": manifest["captured_at"], "returned": bool(candidates)}
                        if scanned
                        else json.loads(prior_expected["evidence_json"])
                    )
                    store.conn.execute(
                        "INSERT INTO expected_keys VALUES(?,?,?,?) ON CONFLICT(scope_id,business_key) DO UPDATE SET eligibility=excluded.eligibility,evidence_json=excluded.evidence_json",
                        (scope_id, key_text(key), eligibility, canonical_json(qualification)),
                    )
                    expected.append(
                        {"key": key, "eligibility": eligibility, "evidence": qualification}
                    )
                    if visible is None:
                        waiting.append({"key": asdict(key), "ready_at": ready_at})
                    else:
                        states.append(visible)
                    if clean is not None:
                        records.append(clean)
        if waiting:
            store.conn.execute(
                "UPDATE runs SET status='BLOCKED',phase='WAITING_DATA',error_json=?,finished_at=? WHERE run_id=?",
                (canonical_json({"reason": "data_not_ready", "waiting": waiting}), now, run_id),
            )
            return {
                "status": "WAITING_DATA",
                "run_id": run_id,
                "scope_id": scope_id,
                "ready_at": max(item["ready_at"] for item in waiting),
                "waiting_count": len(waiting),
            }
        signature = stable_id(
            "signature",
            {
                "states": [(s.record_state_id, s.selected_source_version_id) for s in states],
                "profile": config["profile_id"],
            },
        )
        head = store.head(scope_id)
        counts = {
            "source_new": source_new,
            "clean_new": clean_new,
            "state_new": state_new,
            "expected_reports": len(expected),
            "scan_requested_reports": len(manifest["codes"])
            * len(manifest["periods"])
            * len(FIELDS),
            "scan_returned_reports": len(grouped),
            "evaluated": 0,
            "numeric_changed": 0,
            "status_changed": 0,
            "lineage_changed": 0,
            "reused": 0,
        }
        if head and store.snapshot(head)["signature"] == signature:
            counts["reused"] = store.conn.execute(
                "SELECT COUNT(*) FROM snapshot_features WHERE snapshot_id=?", (head,)
            ).fetchone()[0]
            current_quality = evaluate_quality(
                records,
                states,
                store.features(head),
                profile,
                expected,
                previous_quality(store, scope_id, config["profile_id"], run_id),
            )
            current_quality.update(
                counts=counts,
                capture_id=stable_id(
                    "capture",
                    {
                        "pages": [
                            (page["page_id"], page["raw_hash"]) for page in manifest["pages"]
                        ],
                        "captured_at": manifest["captured_at"],
                    },
                ),
            )
            store.conn.execute(
                "INSERT OR REPLACE INTO quality_metrics VALUES(?,?,?)",
                (run_id, "_report", canonical_json(current_quality)),
            )
            if not current_quality["passed"]:
                store.conn.execute(
                    "UPDATE runs SET status='BLOCKED',phase='VALIDATING',finished_at=?,error_json=? WHERE run_id=?",
                    (now, canonical_json(current_quality["blocking_reasons"]), run_id),
                )
                store.conn.execute(
                    "UPDATE scopes SET health_status='blocked',health_reason=? WHERE scope_id=?",
                    (canonical_json(current_quality["blocking_reasons"]), scope_id),
                )
                return {
                    "status": "BLOCKED",
                    "run_id": run_id,
                    "scope_id": scope_id,
                    "quality": current_quality,
                }
            store.conn.execute(
                "UPDATE runs SET status='SUCCEEDED',phase='DONE',finished_at=?,heartbeat_at=?,snapshot_id=?,counts_json=? WHERE run_id=?",
                (now, now, head, canonical_json(counts), run_id),
            )
            head_quality = json.loads(
                (store.workspace / "snapshots" / head / "quality.json").read_text(encoding="utf-8")
            )
            active = {
                _alert_id(scope_id, event)
                for event in head_quality["events"]
                if event.get("severity") != "info"
            }
            _recover_scope(store, run_id, scope_id, manifest, active)
            return {
                "status": "NO_CHANGE",
                "run_id": run_id,
                "snapshot_id": head,
                "scope_id": scope_id,
                "counts": counts,
                "export_path": str(store.workspace / "snapshots" / head),
            }
    previous_features = store.features(head) if head else []
    same_profile = bool(head and store.snapshot(head)["profile_id"] == config["profile_id"])
    prior_states = (
        {
            row["business_key"]: row["record_state_id"]
            for row in store.conn.execute(
                "SELECT business_key,record_state_id FROM snapshot_records WHERE snapshot_id=?",
                (head,),
            )
        }
        if head
        else {}
    )
    changed_codes = (
        set(scope["codes"])
        if not same_profile
        else {
            state.key.security_code
            for state in states
            if prior_states.get(key_text(state.key)) != state.record_state_id
        }
    )
    # 当前量级按改变公司和冻结有限范围重算，保留精确血缘而不预建字段缓存。
    evaluated = (
        compute_features(
            [record for record in records if record.key.security_code in changed_codes],
            [state for state in states if state.key.security_code in changed_codes],
            tuple(sorted(changed_codes)),
            tuple(scope["output_periods"]),
            now,
            config["profile_id"],
            scope_id,
            profile.get("parameters"),
            formula_version=profile["formula_version"],
        )
        if changed_codes
        else []
    )
    reused = [
        feature for feature in previous_features if feature.security_code not in changed_codes
    ]
    features = sorted(
        evaluated + reused,
        key=lambda feature: (feature.security_code, feature.report_end, feature.feature_name),
    )
    counts["evaluated"], counts["reused"] = len(evaluated), len(reused)
    previous = {(f.security_code, f.report_end, f.feature_name): f for f in previous_features}
    for feature in features:
        old = previous.get((feature.security_code, feature.report_end, feature.feature_name))
        counts["numeric_changed"] += old is None or old.feature_value != feature.feature_value
        counts["status_changed"] += old is None or old.feature_status != feature.feature_status
        counts["lineage_changed"] += (
            old is not None and old.feature_version_id != feature.feature_version_id
        )
    quality = evaluate_quality(
        records,
        states,
        features,
        profile,
        expected,
        previous_quality(store, scope_id, config["profile_id"], run_id),
    )
    quality["counts"] = counts
    quality["capture_id"] = stable_id(
        "capture",
        {
            "pages": [(page["page_id"], page["raw_hash"]) for page in manifest["pages"]],
            "captured_at": manifest["captured_at"],
        },
    )
    clean_by_source = {record.source_version_id: record for record in records}
    with store.transaction():
        blocked = _record_quality(store, run_id, scope_id, head, quality, counts, now)
        if blocked:
            return blocked
        snapshot_id = "snapshot_" + uuid.uuid4().hex
        store.conn.execute(
            "INSERT INTO dataset_snapshots(snapshot_id,scope_id,universe_id,profile_id,run_id,expected_parent_head,data_as_of,status,signature) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                scope_id,
                universe_id,
                config["profile_id"],
                run_id,
                plan["expected_parent_head"],
                now,
                "PREPARED",
                signature,
            ),
        )
        for state in states:
            clean = clean_by_source.get(state.selected_source_version_id)
            key = state.key
            store.conn.execute(
                "INSERT INTO snapshot_records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    key_text(key),
                    key.source,
                    key.security_code,
                    key.report_end,
                    key.statement_type,
                    key.consolidation_scope,
                    state.record_state_id,
                    clean.clean_version_id if clean else None,
                    clean.data_available_at if clean else state.decision_observed_at,
                    clean.source_available_at if clean else None,
                ),
            )
        for feature in features:
            store.save_feature(feature, scope_id, config["profile_id"], now)
            store.conn.execute(
                "INSERT INTO snapshot_features VALUES(?,?,?,?,?)",
                (
                    snapshot_id,
                    feature.security_code,
                    feature.report_end,
                    feature.feature_name,
                    feature.feature_version_id,
                ),
            )
        store.conn.execute(
            "UPDATE runs SET phase='EXPORTING',snapshot_id=?,counts_json=? WHERE run_id=?",
            (snapshot_id, canonical_json(counts), run_id),
        )
    fault("after_prepare")
    return publish_candidate(store, snapshot_id, quality)


def _record_quality(store, run_id, scope_id, head, quality, counts, now) -> dict | None:
    """在调用方的候选事务中保存指标及告警；门禁失败时阻断运行并返回原因。"""
    for metric, value in quality["metrics"].items():
        store.conn.execute(
            "INSERT OR REPLACE INTO quality_metrics VALUES(?,?,?)",
            (run_id, metric, canonical_json(value)),
        )
    store.conn.execute(
        "INSERT OR REPLACE INTO quality_metrics VALUES(?,?,?)",
        (run_id, "_report", canonical_json(quality)),
    )
    for event in quality["events"]:
        context = {
            **event,
            "run_id": run_id,
            "snapshot_parent": head,
            "downstream": ["features_long", "features_panel"],
            "recovery_hint": f'finpipe replay --run-id {run_id} --workspace "{store.workspace}"',
        }
        if event.get("severity") == "info":
            event_id = stable_id("event", {"run": run_id, "event": context})
            store.conn.execute(
                "INSERT OR IGNORE INTO quality_events VALUES(?,?,?,?,?)",
                (
                    event_id,
                    run_id,
                    event.get("rule_id", "quality"),
                    "info",
                    canonical_json(context),
                ),
            )
        else:
            _emit_alert(store, scope_id, run_id, context)
    if not quality["passed"]:
        reasons = canonical_json(quality["blocking_reasons"])
        store.conn.execute(
            "UPDATE runs SET status='BLOCKED',phase='VALIDATING',finished_at=?,counts_json=?,error_json=? WHERE run_id=?",
            (now, canonical_json(counts), reasons, run_id),
        )
        store.conn.execute(
            "UPDATE scopes SET health_status='blocked',health_reason=? WHERE scope_id=?",
            (reasons, scope_id),
        )
        return {
            "status": "BLOCKED",
            "run_id": run_id,
            "scope_id": scope_id,
            "counts": counts,
            "quality": quality,
        }
    return None


def execute_manifest(
    manifest_path: Path,
    config: dict,
    allow_synthetic: bool = False,
    parent_run_id: str | None = None,
    frozen_scope: dict | None = None,
) -> dict:
    """重放固定证据，不联网；人工输入必须显式授权并隔离。"""
    manifest_path = manifest_path.resolve()
    manifest = load_manifest(manifest_path)
    if manifest["capture_kind"] != "real" and not allow_synthetic:
        raise ValueError("人工夹具须显式 --allow-synthetic，并使用独立工作区")
    if frozen_scope:
        if not set(manifest["codes"]) <= set(frozen_scope["codes"]) or not set(
            manifest["periods"]
        ) <= set(frozen_scope["input_periods"]):
            raise ValueError("归档不属于指定冻结范围")
        config = {
            **config,
            "_output_explicit": True,
            "output_periods": tuple(frozen_scope["output_periods"]),
        }
    workspace = Path(config["workspace"])
    with workspace_lock(workspace), Store(workspace) as store:
        copied = _archive_manifest(manifest_path, manifest, workspace)
        scope_manifest = (
            {**manifest, "codes": frozen_scope["codes"], "periods": frozen_scope["input_periods"]}
            if frozen_scope
            else manifest
        )
        scope_id, universe_id, scope = _register(store, scope_manifest, config)
        run_id = _new_run(store, scope_id, config, copied, parent_run_id)
        try:
            from finpipe.ops import finish_diagnostics, ingest_diagnostics

            with store.transaction():
                ingest_diagnostics(store)
            return finish_diagnostics(
                store, _process(store, run_id, manifest, config, scope_id, universe_id, scope)
            )
        except Exception as exc:
            _failed(store, run_id, exc)
            return {"status": "FAILED", "run_id": run_id, "scope_id": scope_id, "error": str(exc)}


def execute_online(config: dict, refresh: str = "all") -> dict:
    """先持写锁并记录任务，再串行联网；最近刷新始终生成原范围完整快照。"""
    workspace = Path(config["workspace"])
    with workspace_lock(workspace), Store(workspace) as store:
        planned = {
            "codes": config["scope"]["codes"],
            "periods": config["input_periods"],
            "capture_kind": "real",
        }
        scope_id, universe_id, scope = _register(store, planned, config)
        capture = workspace / "captures" / ("capture_" + uuid.uuid4().hex)
        run_id = _new_run(store, scope_id, config, capture / "manifest.json")
        periods = (
            tuple(scope["input_periods"][-4:])
            if refresh == "recent" and store.head(scope_id)
            else tuple(scope["input_periods"])
        )
        collection = {"codes": scope["codes"], "periods": list(periods), "refresh": refresh}
        run = store.conn.execute("SELECT plan_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        plan = {**json.loads(run[0]), "collection": collection}
        with store.transaction():
            store.conn.execute(
                "UPDATE runs SET phase='COLLECTING',plan_json=? WHERE run_id=?",
                (canonical_json(plan), run_id),
            )
            from finpipe.ops import ingest_diagnostics

            ingest_diagnostics(store)
        return _collect_run(store, run_id, plan, universe_id)


def _collect_run(store: Store, run_id: str, plan: dict, universe_id: str) -> dict:
    """原计划重试保留失败捕获，每次重试使用新会话而不拼接跨日分页。"""
    from finpipe.ingestion import collect

    def progress(_):
        with store.transaction():
            store.conn.execute("UPDATE runs SET heartbeat_at=? WHERE run_id=?", (utc_now(), run_id))

    capture = store.workspace / "captures" / ("capture_" + uuid.uuid4().hex)
    try:
        _check_cooldown(store)
        plan = {
            **plan,
            "manifest_path": str(capture / "manifest.json"),
            "active_capture": str(capture),
            "collection_attempts": plan.get("collection_attempts", []) + [str(capture)],
        }
        with store.transaction():
            store.conn.execute(
                "UPDATE runs SET plan_json=?,phase='COLLECTING',status='RUNNING',finished_at=NULL,heartbeat_at=? WHERE run_id=?",
                (canonical_json(plan), utc_now(), run_id),
            )
        manifest_path = collect(
            tuple(plan["collection"]["codes"]),
            tuple(plan["collection"]["periods"]),
            capture,
            plan["config"]["network"],
            progress,
        )
        manifest = load_manifest(manifest_path)
        copied = _archive_manifest(manifest_path, manifest, store.workspace)
        plan = {
            **plan,
            "manifest_path": str(copied),
            "capture_history": plan.get("capture_history", []) + [str(copied)],
        }
        with store.transaction():
            store.conn.execute(
                "UPDATE runs SET plan_json=?,phase='PROCESSING',heartbeat_at=?,status='RUNNING',finished_at=NULL WHERE run_id=?",
                (canonical_json(plan), utc_now(), run_id),
            )
        result = _process(
            store, run_id, manifest, plan["config"], plan["scope_id"], universe_id, plan["scope"]
        )
        from finpipe.ops import finish_diagnostics

        return finish_diagnostics(store, result)
    except Exception as exc:
        _failed(store, run_id, exc)
        message = str(exc)
        return {
            "status": "FAILED",
            "run_id": run_id,
            "scope_id": plan["scope_id"],
            "error": message[:300],
            "error_truncated": len(message) > 300,
            "capture_dir": str(capture),
            "manifest_path": str(capture / "manifest.json")
            if (capture / "manifest.json").exists()
            else None,
            "checkpoint_path": str(capture / "checkpoint.json")
            if (capture / "checkpoint.json").exists()
            else None,
            "recovery_hint": f'finpipe resume --run-id {run_id} --workspace "{store.workspace}"',
        }


def _failed(store: Store, run_id: str, error: Exception):
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    snapshot_id = run["snapshot_id"]
    published = snapshot_id and store.snapshot(snapshot_id)["status"] == "PUBLISHED"
    with store.transaction():
        store.conn.execute(
            "UPDATE runs SET status=?,error_json=?,finished_at=? WHERE run_id=?",
            (
                "SUCCEEDED" if published else "FAILED",
                canonical_json({"message": str(error), "published": bool(published)}),
                utc_now(),
                run_id,
            ),
        )
        plan = json.loads(run["plan_json"])
        if not published and store.head(run["scope_id"]) == plan["expected_parent_head"]:
            failure = {
                "rule": "pipeline_failure",
                "rule_id": "pipeline_failure",
                "severity": "error",
                "run_id": run_id,
                "phase": run["phase"],
                "error": str(error),
                "failed_at": utc_now(),
                "requires_new_scan": run["phase"] == "COLLECTING"
                or "capture_incomplete" in str(error),
                "failed_scan": plan.get(
                    "collection",
                    {
                        "codes": plan.get("scope", {}).get("codes", []),
                        "periods": plan.get("scope", {}).get("input_periods", []),
                    },
                ),
                "downstream": ["features_long", "features_panel"],
                "recovery_hint": f'finpipe resume --run-id {run_id} --workspace "{store.workspace}"',
            }
            if failure["requires_new_scan"]:
                failure["rule_id"] = "upstream_capture_failed"
            store.conn.execute(
                "UPDATE scopes SET health_status='blocked',health_reason=? WHERE scope_id=?",
                (canonical_json(failure), run["scope_id"]),
            )
            _emit_alert(store, run["scope_id"], run_id, failure)


def resume_run(workspace: Path, run_id: str) -> dict:
    """读取原冻结计划；新profile不能混入旧失败恢复。"""
    with workspace_lock(workspace), Store(workspace) as store:
        row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("运行不存在")
        plan = json.loads(row["plan_json"])
        if not row["snapshot_id"] and store.head(plan["scope_id"]) != plan["expected_parent_head"]:
            with store.transaction():
                store.conn.execute(
                    "UPDATE runs SET status='SUPERSEDED',phase='DONE',finished_at=? WHERE run_id=?",
                    (utc_now(), run_id),
                )
            return {"status": "SUPERSEDED", "run_id": run_id, "scope_id": plan["scope_id"]}
        if row["snapshot_id"]:
            if store.snapshot(row["snapshot_id"])["status"] == "PUBLISHED":
                from finpipe.ops import finish_diagnostics

                return finish_diagnostics(store, publish_candidate(store, row["snapshot_id"]))
            quality_path = store.workspace / "staging" / row["snapshot_id"] / "quality.json"
            quality = (
                json.loads(quality_path.read_text(encoding="utf-8"))
                if quality_path.exists()
                else None
            )
            if quality is None:
                report = store.conn.execute(
                    "SELECT payload_json FROM quality_metrics WHERE run_id=? AND metric_name='_report'",
                    (run_id,),
                ).fetchone()
                if not report:
                    raise ValueError("候选缺少原质量核验，不能伪造通过恢复")
                quality = json.loads(report[0])
            from finpipe.ops import finish_diagnostics

            return finish_diagnostics(store, publish_candidate(store, row["snapshot_id"], quality))
        if plan.get("collection"):
            path = plan_manifest(store, plan)
            if not path.exists() or not load_manifest(path)["complete"]:
                return _collect_run(
                    store, run_id, plan, store.scope(plan["scope_id"])["universe_id"]
                )
        manifest = load_manifest(plan_manifest(store, plan))
        frozen_scope = plan.get("scope", json.loads(store.scope(plan["scope_id"])["content_json"]))
        scope_manifest = {
            **manifest,
            "codes": frozen_scope["codes"],
            "periods": frozen_scope["input_periods"],
        }
        scope_id, universe_id, scope = _register(store, scope_manifest, plan["config"])
        from finpipe.ops import finish_diagnostics

        return finish_diagnostics(
            store, _process(store, run_id, manifest, plan["config"], scope_id, universe_id, scope)
        )


def _check_cooldown(store: Store):
    """同来源新会话继续遵守上一捕获的冷却，不能靠立即resume绕限流。"""
    row = store.conn.execute(
        "SELECT plan_json FROM runs WHERE json_extract(plan_json,'$.active_capture') IS NOT NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return
    plan = json.loads(row[0])
    old = Path(plan["active_capture"])
    base = Path(plan["config"]["workspace"])
    path = store.workspace / old.relative_to(base) / "checkpoint.json"
    if not path.exists():
        return
    blocked = json.loads(path.read_text(encoding="utf-8")).get("source_blocked") or {}
    if blocked.get("status") == "http_403":
        raise ValueError("上游403为非自动重试错误，请定位访问限制后重新制定采集计划")
    retry_at = blocked.get("next_retry_at")
    if retry_at and iso_time(retry_at) > utc_now():
        raise ValueError(f"retry_deferred：同来源冷却尚未到期，下次可重试时间{retry_at}")
