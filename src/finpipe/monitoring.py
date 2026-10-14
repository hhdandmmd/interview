"""本地告警生命周期：持续去重、复发打开、按恢复证据关闭。"""

import json

from finpipe.identity import canonical_json, stable_id
from finpipe.storage.database import Store
from finpipe.timeutil import iso_time, utc_now


def _emit_alert(store: Store, scope_id: str | None, run_id: str | None, event: dict):
    """持续异常保留确认状态并更新上下文，已恢复问题复发时重新打开。"""
    event_id = event.get("event_id") or stable_id("event", {"run": run_id, "event": event})
    store.conn.execute(
        "INSERT OR IGNORE INTO quality_events VALUES(?,?,?,?,?)",
        (
            event_id,
            run_id,
            event.get("rule_id", "pipeline_failure"),
            event.get("severity", "error"),
            canonical_json(event),
        ),
    )
    identity = _alert_id(scope_id, event)
    now = utc_now()
    store.conn.execute(
        "INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(alert_id) DO UPDATE SET "
        "event_id=excluded.event_id,payload_json=excluded.payload_json,"
        "status=CASE WHEN alerts.status='resolved' THEN 'open' ELSE alerts.status END,"
        "opened_at=CASE WHEN alerts.status='resolved' THEN excluded.opened_at ELSE alerts.opened_at END,"
        "resolved_at=NULL,recovery_run_id=NULL",
        (identity, event_id, scope_id, "open", now, None, None, canonical_json(event)),
    )


def _recover_scope(store: Store, run_id: str, scope_id: str, manifest: dict, active: set[str]):
    """旧离线归档可恢复计算故障，只有真实新扫描能解除采集失败。"""
    scope = store.scope(scope_id)
    unresolved_source = []
    for alert in store.conn.execute(
        "SELECT alert_id,payload_json FROM alerts WHERE scope_id=? AND status!='resolved'",
        (scope_id,),
    ).fetchall():
        payload = json.loads(alert["payload_json"])
        scan = payload.get("failed_scan", {})
        fresh = (
            manifest["capture_kind"] == scope["capture_kind"]
            and manifest["complete"]
            and iso_time(manifest["captured_at"])
            >= iso_time(payload.get("failed_at", manifest["captured_at"]))
            and set(scan.get("codes", [])) <= set(manifest["codes"])
            and set(scan.get("periods", [])) <= set(manifest["periods"])
        )
        if payload.get("requires_new_scan") and not fresh:
            unresolved_source.append(alert["alert_id"])
        if alert["alert_id"] not in active and (not payload.get("requires_new_scan") or fresh):
            store.conn.execute(
                "UPDATE alerts SET status='resolved',resolved_at=?,recovery_run_id=? WHERE alert_id=?",
                (utc_now(), run_id, alert["alert_id"]),
            )
    if unresolved_source:
        store.conn.execute(
            "UPDATE scopes SET health_status='blocked',health_reason=? WHERE scope_id=?",
            (
                canonical_json({"rule": "unresolved_source_failure", "alerts": unresolved_source}),
                scope_id,
            ),
        )
    else:
        store.conn.execute(
            "UPDATE scopes SET health_status='healthy',health_reason=NULL,last_success_at=? WHERE scope_id=?",
            (utc_now(), scope_id),
        )


def _alert_id(scope_id: str | None, event: dict) -> str:
    """同字段连续故障去重，不把不同字段或不同核对混成一个告警。"""
    return stable_id(
        "alert",
        {
            "scope": scope_id,
            "rule": event.get("rule_id"),
            "key": event.get("key"),
            "fields": sorted(event.get("fields", [])),
            "check": event.get("check_name"),
        },
    )


def previous_quality(store: Store, scope_id: str, profile_id: str, run_id: str) -> dict | None:
    """成功且同范围同口径的独立扫描作基线，重复重放不扩充MAD样本。"""
    samples, captures, previous = [], set(), None
    for row in store.conn.execute(
        "SELECT m.payload_json,r.snapshot_id FROM quality_metrics m JOIN runs r USING(run_id) WHERE m.metric_name='_report' AND r.scope_id=? AND r.profile_id=? AND r.status='SUCCEEDED' AND r.run_id<>? ORDER BY r.started_at DESC LIMIT 100",
        (scope_id, profile_id, run_id),
    ):
        report = json.loads(row[0])
        if not report["passed"]:
            continue
        capture = report.get("capture_id", row["snapshot_id"])
        if capture in captures:
            continue
        captures.add(capture)
        previous = previous or report
        samples.append(report["metrics"]["received_count"])
    return {**previous, "successful_row_counts": samples} if previous else None
