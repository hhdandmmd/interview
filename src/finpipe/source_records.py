"""来源身份、观察证据和业务键版本裁决，离线重放不改变捕获事实。"""

import shutil
from dataclasses import asdict, replace
from pathlib import Path

from finpipe.catalog import FIELDS, REPORT_NAMES
from finpipe.exports import sha256, write_json
from finpipe.identity import canonical_json, source_number, stable_id
from finpipe.ingestion import load_manifest
from finpipe.models import BusinessKey, RecordState, SourceRecord
from finpipe.storage.database import Store, decode_state, key_text
from finpipe.timeutil import iso_time, quarter_end
from finpipe.transforms import normalize


def _source_record(raw: dict, table: str, observed_at: str) -> SourceRecord:
    key = BusinessKey(
        "eastmoney", raw["SECUCODE"], quarter_end(str(raw["REPORT_DATE"])[:10]), table
    )
    original = dict(raw)
    evidence = dict(original.get("_evidence", {}))
    for name, value in {
        "consolidation_scope": "consolidated",
        "source_timezone": "Asia/Shanghai",
        "scope_evidence": REPORT_NAMES[table],
    }.items():
        evidence.setdefault(name, value)
    if original.get("CURRENCY") is None:
        evidence.setdefault("currency_assumed", "CNY")
        evidence.setdefault("currency_evidence", "eastmoney_report_spec")
    for field, label in (
        ("NOTICE_DATE", "disclosed_precision"),
        ("UPDATE_DATE", "updated_precision"),
    ):
        value = original.get(field)
        evidence.setdefault(
            label,
            "date"
            if not value or len(str(value)) == 10 or str(value).endswith("00:00:00")
            else "timestamp",
        )
    original["_evidence"] = evidence
    content = {
        spec.source_field: source_number(original.get(spec.source_field)) for spec in FIELDS[table]
    }
    content.update(
        {
            field: original.get(field)
            for field in (
                "SECUCODE",
                "REPORT_DATE",
                "NOTICE_DATE",
                "UPDATE_DATE",
                "CURRENCY",
                "ORG_TYPE",
                "SECURITY_NAME_ABBR",
                "_basis_status",
                "_revision_sequence",
                "_evidence",
            )
        }
    )
    source_id = stable_id("source", {"key": asdict(key), "content": content})
    return SourceRecord(source_id, key, original, iso_time(observed_at))


def _revision(record: SourceRecord, kind: str) -> int | None:
    """人工排序证据仅在明确夹具内使用；真实UPDATE不被默认赋予排序权。"""
    if kind == "synthetic" and record.raw.get("_evidence", {}).get("authoritative_revision"):
        value = record.raw.get("_revision_sequence")
        return value if type(value) is int else None
    return None


def _decide(
    store: Store,
    key: BusinessKey,
    incoming: list[SourceRecord],
    observed_at: str,
    kind: str,
    incoming_times: dict[str, str],
    selection_policy: str = "latest_complete_observation",
) -> RecordState:
    """完整扫描决定当前来源快照；历史观测顺序不冒充源端修订序号。"""
    if selection_policy not in ("latest_complete_observation", "legacy_conflict"):
        raise ValueError("未知来源选择政策")
    previous = store.latest_state(key)
    versions = {record.source_version_id: record for record in incoming}
    current_scan = kind == "real" and selection_policy == "latest_complete_observation"
    latest_observed = max(
        (iso_time(incoming_times[identity]) for identity in versions), default=None
    )
    if current_scan and previous and latest_observed:
        known = store.conn.execute(
            "SELECT MAX(o.observed_at) FROM source_observations o "
            "JOIN source_versions s USING(source_version_id) WHERE s.business_key=?",
            (key_text(key),),
        ).fetchone()[0]
        prior_observed = previous.evidence.get("latest_observed_at")
        known = max((time for time in (known, prior_observed) if time), default=None)
        if known and latest_observed < known:
            return previous
    if previous and incoming and not current_scan:
        if previous.selected_source_version_id:
            prior = store.source(previous.selected_source_version_id)
            versions[prior.source_version_id] = prior
        else:
            for identity in previous.evidence.get("candidate_source_version_ids", []):
                versions[identity] = store.source(identity)
    candidates = sorted(versions)
    if not candidates:
        status, selected, reason = "missing", None, "complete_scan_missing_record"
    elif current_scan:
        if len(candidates) == 1:
            status, selected, reason = "selected", candidates[0], "latest_complete_observation"
        else:
            status, selected, reason = "conflict", None, "multiple_versions_in_complete_scan"
    elif len(candidates) == 1:
        status, selected, reason = "selected", candidates[0], "first_or_unchanged"
    else:
        ranked = [(record, _revision(record, kind)) for record in versions.values()]
        if all(rank is not None for _, rank in ranked):
            maximum = max(rank for _, rank in ranked)
            newest = [record for record, rank in ranked if rank == maximum]
            if len(newest) == 1:
                selected = newest[0].source_version_id
                status, reason = "selected", "verified_revision_sequence"
            else:
                status, selected, reason = "conflict", None, "source_version_conflict"
        else:
            financial = {
                canonical_json(
                    {
                        spec.source_field: source_number(record.raw.get(spec.source_field))
                        for spec in FIELDS[key.statement_type]
                    }
                )
                for record in versions.values()
            }
            if len(financial) == 1 and incoming:
                newest_time = max(incoming_times[record.source_version_id] for record in incoming)
                simultaneous = {
                    record.source_version_id: record
                    for record in incoming
                    if incoming_times[record.source_version_id] == newest_time
                }
                semantics = {
                    canonical_json(
                        {
                            name: record.raw.get(name)
                            for name in (
                                "NOTICE_DATE",
                                "UPDATE_DATE",
                                "CURRENCY",
                                "ORG_TYPE",
                                "_basis_status",
                                "_evidence",
                            )
                        }
                    )
                    for record in simultaneous.values()
                }
                latest = simultaneous[sorted(simultaneous)[0]]
                old = (
                    store.source(previous.selected_source_version_id)
                    if previous and previous.selected_source_version_id
                    else None
                )
                old_observation = (
                    store.conn.execute(
                        "SELECT MAX(observed_at) FROM source_observations WHERE source_version_id=?",
                        (old.source_version_id,),
                    ).fetchone()[0]
                    if old
                    else None
                )
                selected = (
                    old.source_version_id
                    if old and incoming_times[latest.source_version_id] < old_observation
                    else latest.source_version_id
                )
                status, reason = "selected", "metadata_changed"
                if len(semantics) > 1:
                    status, selected, reason = "conflict", None, "simultaneous_metadata_conflict"
            else:
                status, selected, reason = "conflict", None, "source_version_conflict"
    if (
        previous
        and previous.state_status == status
        and previous.selected_source_version_id == selected
    ):
        if (
            status != "conflict"
            or previous.evidence.get("candidate_source_version_ids") == candidates
        ):
            return previous
    sequence = store.conn.execute(
        "SELECT COALESCE(MAX(state_sequence),0)+1 FROM record_states WHERE business_key=?",
        (key_text(key),),
    ).fetchone()[0]
    return RecordState(
        stable_id(
            "state",
            {"key": asdict(key), "sequence": sequence, "status": status, "source": selected},
        ),
        key,
        status,
        selected,
        observed_at,
        reason,
        {
            "candidate_source_version_ids": candidates,
            "capture_kind": kind,
            "complete_scan": True,
            "selection_policy": selection_policy,
            "latest_observed_at": latest_observed,
            "source_revision_verified": reason == "verified_revision_sequence",
        },
    )


def _archive_manifest(path: Path, manifest: dict, workspace: Path) -> Path:
    """把输入证据复制到工作区，不让外部文件删除破坏失败恢复。"""
    directory = (
        workspace
        / "raw"
        / stable_id(
            "capture",
            {
                "pages": [(p["page_id"], p["raw_hash"]) for p in manifest["pages"]],
                "captured_at": manifest["captured_at"],
            },
        )
    )
    directory.mkdir(parents=True, exist_ok=True)
    for page in manifest["pages"] + manifest.get("errors", []):
        for entry in page.get("attempts", [page]):
            if not entry.get("body_path"):
                continue
            source = path.parent / entry["body_path"]
            target = directory / entry["body_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(source, target)
            elif sha256(target) != sha256(source):
                raise ValueError("已有不可变原文内容冲突")
    copied = directory / "manifest.json"
    if not copied.exists():
        write_json(copied, manifest)
    load_manifest(copied)
    return copied


def plan_manifest(store: Store, plan: dict) -> Path:
    """恢复时重定位工作区内证据，原处理口径不随物理目录搬迁改变。"""
    old_path = Path(plan["manifest_path"])
    original_workspace = Path(plan["config"]["workspace"])
    if old_path.is_relative_to(original_workspace):
        return store.workspace / old_path.relative_to(original_workspace)
    raise ValueError("冻结输入未归档到工作区，不能安全重定位")


def _visible_state(
    store: Store, latest: RecordState, profile: dict, cutoff: str, persist_clean: bool = True
):
    """未来正常边界暂保留旧可见状态；发布/查询检查可纯读，不保存新clean。"""
    rows = store.conn.execute(
        "SELECT payload_json FROM record_states WHERE business_key=? ORDER BY state_sequence DESC",
        (key_text(latest.key),),
    ).fetchall()
    ready_at = None
    for row in rows:
        state = decode_state(row[0])
        clean = None
        boundary = state.decision_observed_at
        if state.selected_source_version_id:
            source = store.source(state.selected_source_version_id)
            clean = store.clean(source.source_version_id, profile)
            if clean is None:
                clean = normalize(source, profile, cutoff)
                if persist_clean:
                    store.save_clean(clean, profile)
            boundary = max(boundary, clean.data_available_at)
            clean = replace(clean, data_available_at=boundary)
        if boundary <= cutoff:
            return state, clean, ready_at
        ready_at = boundary if ready_at is None else min(ready_at, boundary)
    return None, None, ready_at


def _attempt_id(page: dict) -> str:
    """成功页沿用稳定证据身份，重复离线重放不伪造新的HTTP观察。"""
    return stable_id("attempt", {"page_id": page["page_id"], "observed_at": page["observed_at"]})


def _save_attempts(store: Store, run_id: str, manifest: dict, path: Path):
    """完整保存成功、重试和失败请求；失败扫描不能产生财报缺失裁决。"""
    for group in manifest["pages"] + manifest.get("errors", []):
        request = group.get("request", {})
        batch_id = stable_id(
            "batch", {"run": run_id, "request": request, "group": group.get("group_id")}
        )
        success = group in manifest["pages"]
        store.conn.execute(
            "INSERT OR IGNORE INTO batches VALUES(?,?,?,?)",
            (batch_id, run_id, "succeeded" if success else "failed", canonical_json(request)),
        )
        for index, entry in enumerate(group.get("attempts") or [group], 1):
            current = dict(entry)
            current.setdefault("request", request)
            current.setdefault("http_status", group.get("http_status"))
            if (
                success
                and current.get("raw_hash") == group["raw_hash"]
                and current.get("observed_at") == group["observed_at"]
            ):
                identity = _attempt_id(group)
            else:
                identity = stable_id(
                    "attempt", {"request": request, "entry": current, "index": index}
                )
            body = str(path.parent / current["body_path"]) if current.get("body_path") else None
            store.conn.execute(
                "INSERT OR IGNORE INTO http_attempts VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    batch_id,
                    int(request.get("params", {}).get("pageNumber", 1)),
                    current.get("attempt", index),
                    current.get("observed_at"),
                    body,
                    current.get("raw_hash"),
                    canonical_json(current),
                ),
            )
