"""独立只读健康诊断、精确产物检查与一致备份。"""

import json
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from finpipe.exports import publication_receipt, sha256, validate_files, write_json
from finpipe.identity import canonical_json, stable_id
from finpipe.runtime import workspace_lock
from finpipe.storage.database import Store
from finpipe.timeutil import parse_time, utc_now


def health(workspace: Path, job: dict | None = None) -> dict:
    """不取业务锁、不写数据库；失败诊断仍落独立文件。"""
    now = utc_now()
    requested_job = job
    job = job or {"period_seconds": 86400, "grace_seconds": 900, "max_run_seconds": 7200}
    rules = []
    try:
        with Store(workspace, readonly=True) as store:
            scopes = [dict(row) for row in store.conn.execute("SELECT * FROM scopes")]
            run = store.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if run and requested_job is None:
                job = json.loads(run["plan_json"])["config"]["job"]
            if not run:
                rules.append("never_started")
            else:
                elapsed = (parse_time(now) - parse_time(run["heartbeat_at"])).total_seconds()
                if run["status"] == "RUNNING" and elapsed > job["max_run_seconds"]:
                    rules.append("run_timeout")
                if run["status"] in ("FAILED", "BLOCKED", "INTERRUPTED"):
                    rules.append("last_run_failed")
            for scope in scopes:
                scheduled = store.conn.execute(
                    "SELECT * FROM runs WHERE scope_id=? AND json_extract(plan_json,'$.collection') IS NOT NULL ORDER BY started_at DESC LIMIT 1",
                    (scope["scope_id"],),
                ).fetchone()
                latest = store.conn.execute(
                    "SELECT * FROM runs WHERE scope_id=? ORDER BY started_at DESC LIMIT 1",
                    (scope["scope_id"],),
                ).fetchone()
                task = scheduled or latest
                task_job = requested_job or (
                    json.loads(task["plan_json"])["config"]["job"] if task else job
                )
                scope["scheduled_run_id"] = scheduled["run_id"] if scheduled else None
                scope["job_spec"] = task_job
                if not task:
                    rules.append("never_started")
                elif (parse_time(now) - parse_time(task["started_at"])).total_seconds() > task_job[
                    "period_seconds"
                ] + task_job["grace_seconds"]:
                    rules.append("missed_start")
                if scheduled and scheduled["status"] in ("FAILED", "BLOCKED", "INTERRUPTED"):
                    rules.append("last_run_failed")
                if (
                    task
                    and task["status"] == "RUNNING"
                    and (parse_time(now) - parse_time(task["heartbeat_at"])).total_seconds()
                    > task_job["max_run_seconds"]
                ):
                    rules.append("run_timeout")
                if scope["health_status"] != "healthy":
                    rules.append("scope_blocked")
                head = store.head(scope["scope_id"])
                if not head:
                    rules.append("no_published_snapshot")
                else:
                    validate_published(store, head)
                    from finpipe.publication import snapshot_sources_current

                    scope["source_current"] = snapshot_sources_current(store, head)
                    if not scope["source_current"]:
                        rules.append("source_dependency_stale")
            result = {
                "status": "healthy" if not rules else "unhealthy",
                "diagnostic_complete": True,
                "checked_at": now,
                "rules": sorted(set(rules)),
                "scopes": scopes,
                "last_run": dict(run) if run else None,
            }
    except (sqlite3.Error, OSError, ValueError) as exc:
        result = {
            "status": "unknown",
            "diagnostic_complete": False,
            "checked_at": now,
            "rules": ["database_or_artifact_unavailable"],
            "error": str(exc),
        }
    result["event_id"] = stable_id(
        "health",
        {"workspace": str(workspace.resolve()), "checked_at": now, "rules": result["rules"]},
    )
    result["persisted"] = False
    write_json(workspace / "diagnostics" / (result["event_id"] + ".json"), result)
    return result


def ingest_diagnostics(store: Store):
    """逐次诊断留审计，只用最新事实更新全局告警，旧文件不制造复发。"""
    diagnostics = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (store.workspace / "diagnostics").glob("health_*.json")
    ]
    for result in diagnostics:
        if result["status"] == "healthy":
            continue
        event_id = result["event_id"]
        store.conn.execute(
            "INSERT OR IGNORE INTO quality_events VALUES(?,?,?,?,?)",
            (event_id, None, "health", "error", canonical_json(result)),
        )
    latest = max(diagnostics, key=lambda result: result["checked_at"], default=None)
    if latest and latest["status"] != "healthy":
        from finpipe.monitoring import _emit_alert

        _emit_alert(store, None, None, {**latest, "rule_id": "health", "severity": "error"})
    if latest and latest["status"] == "healthy" and latest["diagnostic_complete"]:
        recovery = store.conn.execute(
            "SELECT run_id FROM runs WHERE status='SUCCEEDED' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if recovery:
            store.conn.execute(
                "UPDATE alerts SET status='resolved',resolved_at=?,recovery_run_id=? WHERE scope_id IS NULL AND status!='resolved' AND opened_at<=?",
                (latest["checked_at"], recovery[0], latest["checked_at"]),
            )


def finish_diagnostics(store: Store, result: dict) -> dict:
    """成功处理后实际复核产物及任务状态，健康告警按新诊断恢复。"""
    if result["status"] in ("PUBLISHED", "NO_CHANGE"):
        diagnostic = health(store.workspace)
        with store.transaction():
            ingest_diagnostics(store)
        result["scope_health"] = store.scope(result["scope_id"])["health_status"]
        if result["scope_health"] == "healthy" and any(
            scope["scope_id"] == result["scope_id"] and not scope.get("source_current", True)
            for scope in diagnostic.get("scopes", [])
        ):
            result["scope_health"] = "stale"
        result["health_rules"] = diagnostic["rules"]
    return result


def doctor(workspace: Path, repair: bool = False) -> dict:
    """诊断完整性；修复仅补派生回执、已发布运行结果和诊断。"""
    lock = workspace_lock(workspace) if repair else None
    if lock:
        lock.acquire()
    try:
        with Store(workspace, readonly=not repair) as store:
            integrity = [row[0] for row in store.conn.execute("PRAGMA integrity_check")]
            foreign_keys = [list(row) for row in store.conn.execute("PRAGMA foreign_key_check")]
            failures, repaired = [], []
            from finpipe.ingestion import load_manifest

            for path in (workspace / "raw").glob("*/manifest.json"):
                try:
                    load_manifest(path)
                except (OSError, ValueError) as exc:
                    failures.append({"manifest": str(path), "error": str(exc)})
            for row in store.conn.execute(
                "SELECT * FROM dataset_snapshots WHERE status='PUBLISHED'"
            ):
                try:
                    validate_published(store, row["snapshot_id"])
                    if repair:
                        publication_receipt(store, row["snapshot_id"])
                        with store.transaction():
                            store.conn.execute(
                                "UPDATE runs SET status='SUCCEEDED',phase='DONE' WHERE run_id=?",
                                (row["run_id"],),
                            )
                        repaired.append(row["snapshot_id"])
                except (OSError, ValueError) as exc:
                    failures.append({"snapshot_id": row["snapshot_id"], "error": str(exc)})
            if repair:
                with store.transaction():
                    ingest_diagnostics(store)
            return {
                "status": "ok"
                if integrity == ["ok"] and not foreign_keys and not failures
                else "failed",
                "integrity": integrity,
                "foreign_keys": foreign_keys,
                "failures": failures,
                "repaired": repaired,
            }
    finally:
        if lock:
            lock.release()


def backup(workspace: Path, output: Path) -> dict:
    """协调固定文件集合与SQLite backup接口，不复制活动库文件冒充一致。"""
    output = output.resolve()
    destination = output / ("backup_" + uuid.uuid4().hex)
    if destination.is_relative_to(workspace.resolve()):
        if (
            output == workspace.resolve()
            or output.is_relative_to(workspace / "raw")
            or output.is_relative_to(workspace / "snapshots")
        ):
            raise ValueError("备份不能写进原文或发布目录")
    with workspace_lock(workspace), Store(workspace, readonly=True) as store:
        destination.mkdir(parents=True)
        with closing(sqlite3.connect(destination / "catalog.sqlite")) as target:
            store.conn.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库完整性检查失败")
        for name in ("raw", "snapshots"):
            source = workspace / name
            if source.exists():
                shutil.copytree(source, destination / name)
        write_json(
            destination / "backup.json",
            {"created_at": utc_now(), "source_workspace": str(workspace), "schema_version": 1},
        )
    result = doctor(destination)
    return {"status": result["status"], "backup_path": str(destination), "verification": result}


def validate_published(store: Store, snapshot_id: str):
    """把文件清单与SQL封存摘要锚定，合法JSON也不能静默篡改。"""
    snapshot = store.snapshot(snapshot_id)
    directory = store.workspace / "snapshots" / snapshot_id
    manifest = validate_files(directory, snapshot_id)
    if sha256(directory / "manifest.json") != snapshot["manifest_hash"]:
        raise ValueError("封存manifest摘要与SQL发布事实不一致")
    for name in ("scope_id", "profile_id", "universe_id", "data_as_of"):
        if manifest.get(name) != snapshot[name]:
            raise ValueError("已发布清单与SQL元数据不一致")
    return manifest
