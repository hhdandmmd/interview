"""串行低频财报采集：有限瞬态重试与同源冷却共享一个会话。"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests

from finpipe.catalog import REPORT_NAMES
from finpipe.identity import stable_id

from .archive import (
    ManifestError,
    archive_body,
    decode_page,
    load_manifest,
    save_manifest,
    utc_now,
    validate_pagination,
)

ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


def _save_checkpoint(directory: Path, checkpoint: dict) -> None:
    """同目录原子替换进度文件；它只用于定位，不冒充最终冻结清单。"""
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint["captured_at"] = utc_now()
    temporary = directory / "checkpoint.json.tmp"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(checkpoint, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "checkpoint.json")


def _next_retry_at(delay: float) -> str | None:
    """给可表示的真实冷却计算恢复时间，极大等待保留未知而不截短。"""
    if not math.isfinite(delay) or delay >= 365 * 86400:
        return None
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=delay))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def retry_after(value: str | None, now: datetime) -> float | None:
    """兼容秒数和 HTTP 日期；非法头交由有限指数退避处理。"""
    if value is None:
        return None
    value = value.strip()
    if value.isdecimal():
        try:
            return float(value)
        except OverflowError:
            return math.inf
    try:
        target = parsedate_to_datetime(value)
        target = target if target.tzinfo else target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class SerialClient:
    """单来源所有请求共用等待状态；长限流和 403 不得被下一批请求绕过。"""

    def __init__(self, settings: dict, archive_dir: Path, on_attempt: Callable | None = None):
        self.directory = archive_dir
        self.endpoint = settings.get("endpoint", ENDPOINT)
        self.interval = max(1.0, float(settings.get("interval", 1.0)))
        self.timeout = float(settings.get("timeout", 30.0))
        self.max_attempts = int(settings.get("max_attempts", 3))
        self.max_wait = float(settings.get("max_wait", 30.0))
        if not all(
            math.isfinite(x) and x > 0 for x in (self.interval, self.timeout, self.max_wait)
        ):
            raise ValueError("网络等待和超时必须为有限正数")
        if self.max_attempts < 1 or self.max_attempts > 5:
            raise ValueError("网络尝试次数必须在 1 到 5 之间")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://data.eastmoney.com/",
            }
        )
        self.blocked: dict | None = None
        self.cooldown: dict | None = None
        self.next_delay = self.interval
        self.on_attempt = on_attempt

    def _progress(self, event: dict) -> None:
        """每次尝试记录真实上下文；同步冷却后再通知心跳，包含失败尝试。"""
        if self.on_attempt:
            self.on_attempt({**event, "source_blocked": self.blocked or self.cooldown})

    def close(self) -> None:
        """释放连接池，成功与失败均由采集编排调用。"""
        self.session.close()

    def get(self, params: dict) -> tuple[bytes | None, dict]:
        """每次 HTTP 响应先归档字节，再返回成功正文或带证据的错误。"""
        request = {"endpoint": self.endpoint, "params": dict(params)}
        if self.blocked is not None:
            skipped = {
                "status": "not_attempted",
                "request": request,
                "blocked_by": self.blocked,
                "decision_observed_at": utc_now(),
            }
            self._progress(skipped)
            return None, skipped
        attempts = []
        for attempt in range(1, self.max_attempts + 1):
            time.sleep(self.next_delay)
            self.next_delay = self.interval
            self.cooldown = None
            started_at = utc_now()
            self._progress(
                {
                    "status": "requesting",
                    "request": request,
                    "attempt": attempt,
                    "started_at": started_at,
                }
            )
            try:
                response = self.session.get(
                    self.endpoint,
                    params=params,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.exceptions.SSLError as exc:
                error = {
                    "status": "request_error",
                    "request": request,
                    "error": str(exc),
                    "attempt": attempt,
                    "started_at": started_at,
                    "observed_at": utc_now(),
                }
                attempts.append(error)
                self._progress(error)
                return None, {**error, "attempts": attempts}
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                error = {
                    "status": "network_error",
                    "request": request,
                    "started_at": started_at,
                    "observed_at": utc_now(),
                    "error": str(exc),
                    "attempt": attempt,
                }
                attempts.append(error)
                # 永久 DNS 失败不会通过立即重试恢复；临时解析失败才属于瞬态。
                cause = exc
                permanent_dns = False
                visited = set()
                while cause is not None and id(cause) not in visited:
                    visited.add(id(cause))
                    if isinstance(cause, socket.gaierror) and cause.errno != socket.EAI_AGAIN:
                        permanent_dns = True
                    cause = cause.__cause__ or cause.__context__
                if permanent_dns or attempt == self.max_attempts:
                    self._progress(error)
                    return None, {**error, "attempts": attempts}
                self.next_delay = max(self.interval, float(2**attempt))
                self.cooldown = {
                    "status": "retry_wait",
                    "next_retry_at": _next_retry_at(self.next_delay),
                    "retry_after": None,
                }
                self._progress(error)
                continue
            except requests.exceptions.RequestException as exc:
                error = {
                    "status": "request_error",
                    "request": request,
                    "error": str(exc),
                    "attempt": attempt,
                    "started_at": started_at,
                    "observed_at": utc_now(),
                }
                attempts.append(error)
                self._progress(error)
                return None, {**error, "attempts": attempts}
            body = response.content
            observed_at = utc_now()
            relative, digest = archive_body(self.directory, body)
            evidence = {
                "body_path": relative,
                "raw_hash": digest,
                "observed_at": observed_at,
                "started_at": started_at,
                "request": request,
                "http_status": response.status_code,
                "attempt": attempt,
            }
            status = response.status_code
            header = response.headers.get("Retry-After")
            response.close()
            if status == 200:
                attempts.append(dict(evidence))
                self._progress({**evidence, "status": "response_received"})
                return body, {**evidence, "attempts": attempts}
            evidence.update(status="http_error", retry_after=header)
            attempts.append(dict(evidence))
            evidence["attempts"] = attempts
            if status not in RETRYABLE_HTTP:
                if status == 403:
                    self.blocked = {
                        "status": "http_403",
                        "observed_at": observed_at,
                        "next_retry_at": None,
                        "retry_after": header,
                        "manual_review_required": True,
                    }
                self._progress({key: value for key, value in evidence.items() if key != "attempts"})
                return None, evidence
            delay = max(
                self.interval,
                float(2**attempt),
                retry_after(header, datetime.now(timezone.utc)) or 0,
            )
            if delay > self.max_wait:
                retry_at = _next_retry_at(delay)
                evidence.update(status="retry_deferred", next_retry_at=retry_at)
                self.blocked = {
                    "status": "retry_deferred",
                    "next_retry_at": retry_at,
                    "retry_after": header,
                }
                self._progress({key: value for key, value in evidence.items() if key != "attempts"})
                return None, evidence
            self.next_delay = delay
            self.cooldown = {
                "status": "retry_wait",
                "next_retry_at": _next_retry_at(delay),
                "retry_after": header,
            }
            if attempt == self.max_attempts:
                # 最后一次限流也约束后续任务；不能换公司立即再次请求。
                if status == 429:
                    retry_at = _next_retry_at(delay)
                    evidence["next_retry_at"] = retry_at
                    self.blocked = {
                        "status": "retry_exhausted",
                        "retry_after": header,
                        "next_retry_at": retry_at,
                    }
                self._progress({key: value for key, value in evidence.items() if key != "attempts"})
                return None, evidence
            self._progress({key: value for key, value in evidence.items() if key != "attempts"})
        raise AssertionError("有限请求循环未返回")


def period_groups(periods: tuple[str, ...]) -> list[tuple[str, ...]]:
    """将连续季末合为一个范围，稀疏选择拆开以避免采入未请求期间。"""
    groups: list[list[str]] = []
    previous = None
    for period in sorted(set(periods)):
        parsed = date.fromisoformat(period)
        if (parsed.month, parsed.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
            raise ValueError("报告期必须为季末 ISO 日期")
        ordinal = parsed.year * 4 + parsed.month // 3
        if previous is None or ordinal != previous + 1:
            groups.append([])
        groups[-1].append(period)
        previous = ordinal
    return [tuple(group) for group in groups]


def collect(
    codes: tuple[str, ...],
    periods: tuple[str, ...],
    archive_dir: Path,
    settings: dict,
    on_progress: Callable | None = None,
) -> Path:
    """采集冻结范围的三张完整报表；任一失败留档并返回不完整清单。"""
    if not codes or not periods or len(codes) != len(set(codes)):
        raise ValueError("采集公司和期间不能为空，公司不能重复")
    if any(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) is None for code in codes):
        raise ValueError("证券代码必须包含六位代码和交易所后缀")
    if any(
        (archive_dir / name).exists()
        for name in ("manifest.json", "checkpoint.json", "checkpoint.json.tmp")
    ):
        raise FileExistsError("已有冻结清单或中断进度，请使用新的采集目录，不能跨会话拼页")
    groups = period_groups(periods)
    page_size = int(settings.get("page_size", 50))
    if not 1 <= page_size <= 500:
        raise ValueError("page_size 必须在 1 到 500 之间")
    manifest = {
        "schema_version": 1,
        "source": "eastmoney",
        "codes": list(codes),
        "periods": sorted(set(periods)),
        "captured_at": utc_now(),
        "capture_kind": "real",
        "pages": [],
        "complete": False,
        "errors": [],
    }
    planned_groups = {
        stable_id("request", [code, name, group]): {
            "group_id": stable_id("request", [code, name, group]),
            "codes": [code],
            "periods": list(group),
            "report_type": kind,
            "report_name": name,
            "status": "pending",
            "page_ids": [],
        }
        for code in codes
        for kind, name in REPORT_NAMES.items()
        for group in groups
    }
    checkpoint = {
        "schema_version": 1,
        "checkpoint_kind": "capture_progress",
        "source": "eastmoney",
        "capture_kind": "real",
        "capture_directory": str(archive_dir.resolve()),
        "codes": list(codes),
        "periods": sorted(set(periods)),
        "created_at": utc_now(),
        "phase": "COLLECTING",
        "complete": False,
        "capture_complete": False,
        "request_groups": list(planned_groups.values()),
        "completed_groups": [],
        "pages": [],
        "errors": [],
        "attempts": [],
        "current_request": None,
        "source_blocked": None,
    }

    def attempt_progress(event: dict) -> None:
        """请求前及每次真实结果均先落进度，再通知外层任务心跳。"""
        if event["status"] == "not_attempted":
            checkpoint["last_skipped_request"] = event
        else:
            checkpoint["current_request"] = event
        checkpoint["source_blocked"] = event["source_blocked"]
        if event["status"] not in {"requesting", "not_attempted"}:
            checkpoint["attempts"].append(event)
        _save_checkpoint(archive_dir, checkpoint)
        if on_progress:
            on_progress(event)

    def record_error(error: dict) -> None:
        """错误索引不复制原文或完整行；文件摘要与路径仍可定位真实证据。"""
        manifest["errors"].append(error)
        checkpoint["errors"].append(
            {key: value for key, value in error.items() if key != "attempts"}
        )
        _save_checkpoint(archive_dir, checkpoint)

    _save_checkpoint(archive_dir, checkpoint)
    client = SerialClient(settings, archive_dir, attempt_progress)
    try:
        for code in codes:
            for report_type, report_name in REPORT_NAMES.items():
                for group in groups:
                    group_id = stable_id("request", [code, report_name, group])
                    group_progress = planned_groups[group_id]
                    group_progress["status"] = "scanning"
                    checkpoint["active_group"] = group_id
                    _save_checkpoint(archive_dir, checkpoint)
                    collected = []
                    total_pages = 1
                    number = 1
                    while number <= total_pages:
                        params = {
                            "reportName": report_name,
                            "columns": "ALL",
                            "pageNumber": str(number),
                            "pageSize": str(page_size),
                            "sortColumns": "REPORT_DATE",
                            "sortTypes": "-1",
                            "filter": f"(REPORT_DATE>='{group[0]}')(REPORT_DATE<='{group[-1]}')(SECUCODE=\"{code}\")",
                            "source": "WEB",
                            "client": "WEB",
                        }
                        body, evidence = client.get(params)
                        evidence.update(
                            report_type=report_type, report_name=report_name, group_id=group_id
                        )
                        if body is None:
                            group_progress["status"] = "failed"
                            record_error(evidence)
                            break
                        try:
                            decoded = decode_page(body, report_type, {code}, set(group))
                        except ManifestError as exc:
                            evidence.update(status="schema_error", error=str(exc))
                            group_progress["status"] = "failed"
                            record_error(evidence)
                            break
                        page = {
                            **evidence,
                            **decoded,
                            "requested_codes": [code],
                            "requested_periods": list(group),
                            "page_id": stable_id(
                                "page",
                                [group_id, number, evidence["raw_hash"], evidence["observed_at"]],
                            ),
                        }
                        collected.append(page)
                        manifest["pages"].append(page)
                        checkpoint["pages"].append(
                            {
                                **{
                                    key: value
                                    for key, value in page.items()
                                    if key not in {"rows", "attempts"}
                                },
                                "rows_received": len(page["rows"]),
                            }
                        )
                        group_progress["page_ids"].append(page["page_id"])
                        total_pages = max(1, decoded["pages"])
                        group_progress["expected_pages"] = decoded["pages"]
                        _save_checkpoint(archive_dir, checkpoint)
                        if total_pages > 100:
                            group_progress["status"] = "failed"
                            record_error(
                                {
                                    "status": "schema_error",
                                    "group_id": group_id,
                                    "error": "窄范围请求返回异常页数",
                                }
                            )
                            break
                        if on_progress:
                            on_progress(
                                {
                                    "code": code,
                                    "report_type": report_type,
                                    "page": number,
                                    "pages": total_pages,
                                    "rows": len(decoded["rows"]),
                                    "status": "page_validated",
                                }
                            )
                        number += 1
                    if number > total_pages:
                        try:
                            validate_pagination(collected)
                            group_progress["status"] = "completed"
                            checkpoint["completed_groups"].append(group_id)
                        except ManifestError as exc:
                            group_progress["status"] = "failed"
                            record_error(
                                {
                                    "status": "pagination_error",
                                    "group_id": group_id,
                                    "error": str(exc),
                                }
                            )
                    _save_checkpoint(archive_dir, checkpoint)
        expected = {
            (code, period, kind) for code in codes for period in periods for kind in REPORT_NAMES
        }
        actual = {
            (row["SECUCODE"], str(row["REPORT_DATE"])[:10], page["report_type"])
            for page in manifest["pages"]
            for row in page["rows"]
        }
        manifest["missing_keys"] = [
            {"security_code": code, "report_end": period, "report_type": kind}
            for code, period, kind in sorted(expected - actual)
        ]
        if not actual:
            record_error({"status": "empty_batch", "error": "整批请求未获得财报记录"})
        manifest["complete"] = not manifest["errors"]
        manifest["captured_at"] = utc_now()
        path = save_manifest(archive_dir, manifest)
        load_manifest(path)
        checkpoint.update(
            phase="FINISHED", capture_complete=manifest["complete"], manifest_path="manifest.json"
        )
        _save_checkpoint(archive_dir, checkpoint)
        return path
    except BaseException as exc:
        checkpoint.update(
            phase="INTERRUPTED", interruption={"type": type(exc).__name__, "message": str(exc)}
        )
        _save_checkpoint(archive_dir, checkpoint)
        raise
    finally:
        client.close()
