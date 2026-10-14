"""低频验证公开财报接口，保存解析后的源 JSON 及请求证据。"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30.0
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
SOURCE_TIMEZONE = timezone(timedelta(hours=8))
REPORTS = {
    "RPT_F10_FINANCE_GINCOME": ("REPORT_DATE", ("OPERATE_INCOME", "NETPROFIT")),
    "RPT_F10_FINANCE_GBALANCE": (
        "REPORT_DATE",
        ("TOTAL_ASSETS", "TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIAB"),
    ),
    "RPT_F10_FINANCE_MAINFINADATA": ("REPORT_DATE", ("ROEJQ", "XSMLL", "LD")),
}
SUMMARIES = {
    "RPT_DMSK_FN_INCOME": ("REPORT_DATE", ("TOTAL_OPERATE_INCOME",)),
    "RPT_DMSK_FN_BALANCE": ("REPORT_DATE", ("CURRENT_RATIO",)),
    "RPT_LICO_FN_CPD": ("REPORTDATE", ("WEIGHTAVG_ROE", "XSMLL")),
}
BANK_REPORT = {
    "RPT_F10_FINANCE_BBALANCE": ("REPORT_DATE", ("TOTAL_ASSETS", "TOTAL_LIABILITIES")),
}


def source_date(value: object) -> date:
    """校验 ISO 日期或时间，并按上海时区返回来源日期。"""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected an ISO date or timestamp")
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value)
    return (parsed.astimezone(SOURCE_TIMEZONE) if parsed.tzinfo else parsed).date()


def validate_payload(
    payload: object,
    period_key: str,
    fields: tuple[str, ...],
    code: str,
    period: str,
    observed: datetime,
) -> dict:
    """核验单一公司、报告期的响应身份与字段，保留源值，不做财务清洗。"""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return {"status": "api_error"}
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return {"status": "schema_error"}
    rows, count, pages = result["data"], result.get("count"), result.get("pages")
    # 精确单记录请求出现额外版本或行数不一致时，不能只取第一行报成功。
    if (
        type(count) is not int
        or type(pages) is not int
        or count != len(rows)
        or len(rows) > 1
        or pages != (1 if rows else 0)
        or not all(isinstance(row, dict) for row in rows)
    ):
        return {"status": "schema_error", "error": "Inconsistent rows/count/pages"}
    if not rows:
        return {"status": "empty"}
    row = rows[0]
    missing = sorted({"SECUCODE", period_key, "NOTICE_DATE", *fields} - row.keys())
    if missing:
        return {"status": "schema_error", "missing_fields": missing}
    invalid = []
    for field in fields:
        value = row[field]
        if value is None:
            continue  # 可空字段保留未披露事实；缺失值不等于零。
        try:
            if isinstance(value, bool) or not math.isfinite(float(value)):
                invalid.append(field)
        except (TypeError, ValueError, OverflowError):
            invalid.append(field)
    try:
        report_date = source_date(row[period_key])
    except ValueError:
        invalid.append(period_key)
        report_date = None
    try:
        notice = source_date(row["NOTICE_DATE"])
        if notice > observed.astimezone(SOURCE_TIMEZONE).date() or (
            report_date is not None and notice < report_date
        ):
            invalid.append("NOTICE_DATE")
        if len(row["NOTICE_DATE"]) > 10:
            notice_time = datetime.fromisoformat(row["NOTICE_DATE"])
            # 来源午夜通常是日期占位；非午夜时间须按完整时点核验，不能截日。
            if any((notice_time.hour, notice_time.minute, notice_time.second, notice_time.microsecond)):
                notice_time = notice_time if notice_time.tzinfo else notice_time.replace(tzinfo=SOURCE_TIMEZONE)
                if notice_time > observed and "NOTICE_DATE" not in invalid:
                    invalid.append("NOTICE_DATE")
    except ValueError:
        invalid.append("NOTICE_DATE")
    if invalid:
        return {"status": "schema_error", "invalid_fields": sorted(invalid)}
    if row["SECUCODE"] != code or report_date.isoformat() != period:
        return {"status": "identity_error"}
    return {"status": "ok", "missing_fields": []}


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """解析 HTTP 等待秒数或日期；非法响应头返回空，由调用方使用退避。"""
    if value is None:
        return None
    value = value.strip()
    if value.isdecimal():
        try:
            return float(value)
        except OverflowError:
            return math.inf
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def transient_network_error(exc: BaseException) -> bool:
    """识别可有限重试的网络故障；永久 DNS 或证书错误不靠重试掩盖。"""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (TimeoutError, ConnectionError, http.client.IncompleteRead)):
        return True
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    return isinstance(reason, OSError) and reason.errno in {
        errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED,
        errno.ENETUNREACH, errno.EHOSTUNREACH,
    }


def fetch(
    report: str,
    specification: tuple[str, tuple[str, ...]],
    code: str,
    args: argparse.Namespace,
) -> dict:
    """按报表规格验证一个业务键，记录每次尝试；长限流返回延后状态。"""
    period_key, fields = specification
    params = {
        "reportName": report,
        "columns": "ALL",
        "pageNumber": "1",
        "pageSize": "1",
        "filter": f'({period_key}=\'{args.period}\')(SECUCODE="{code}")',
        "source": "WEB",
        "client": "WEB",
    }
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://data.eastmoney.com/",
    })
    evidence = {"report": report, "code": code, "url": url, "attempts": []}
    delay = 1.0
    for attempt in range(MAX_ATTEMPTS):
        time.sleep(delay)
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                payload = json.load(response)
            observed = datetime.now(timezone.utc)
            evidence["attempts"].append({
                "at_utc": stamp, "observed_at_utc": observed.isoformat(), "payload": payload,
            })
            evidence.update(validate_payload(payload, period_key, fields, code, args.period, observed))
            return evidence
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            evidence["attempts"].append({"at_utc": stamp, "error": str(exc)})
            evidence["status"] = "schema_error"
            return evidence
        except urllib.error.HTTPError as exc:
            now = datetime.now(timezone.utc)
            header = exc.headers.get("Retry-After") if exc.headers else None
            evidence["attempts"].append({
                "at_utc": stamp, "error": str(exc), "http_status": exc.code,
                "retry_after": header,
            })
            exc.close()
            if exc.code not in RETRYABLE_HTTP:
                evidence["status"] = "http_error"
                return evidence
            retry_after = retry_after_seconds(header, now)
            delay = max(2 ** (attempt + 1), retry_after or 0.0)
            if delay > MAX_RETRY_WAIT:
                evidence["status"] = "retry_deferred"
                evidence["retry_after_seconds"] = delay if math.isfinite(delay) else None
                # 长限流交给后续运行恢复，不能为缩短探测而提前请求。
                evidence["next_retry_at"] = (
                    (now + timedelta(seconds=delay)).isoformat()
                    if math.isfinite(delay) and delay <= 86400 * 365
                    else None
                )
                return evidence
        except (
            urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead
        ) as exc:
            evidence["attempts"].append({"at_utc": stamp, "error": str(exc)})
            if not transient_network_error(exc):
                evidence["status"] = "request_error"
                return evidence
            delay = float(2 ** (attempt + 1))
    evidence["status"] = "request_error"
    return evidence


def main() -> int:
    """执行固定探测计划并保存指定证据；任一失败或未请求项返回非零码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codes", nargs="+", default=["600519.SH", "000333.SZ", "000651.SZ"]
    )
    parser.add_argument("--period", default="2024-12-31")
    parser.add_argument("--timeout", type=float, default=8)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--summaries", action="store_true",
        help="Compare summary endpoints for the first code",
    )
    selection.add_argument("--report", choices=REPORTS | SUMMARIES | BANK_REPORT)
    parser.add_argument("--output", type=Path, default=Path("data/evidence/source_probe.json"))
    args = parser.parse_args()
    if any(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) is None for code in args.codes):
        parser.error("Codes must use the form 600519.SH, 000333.SZ or 920xxx.BJ")
    try:
        period = date.fromisoformat(args.period)
    except ValueError:
        parser.error("Period must be an ISO date")
    if (period.month, period.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        parser.error("Period must be a quarter end")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("Timeout must be finite and positive")

    checks = []
    deferred = None
    for code in args.codes:
        if args.report:
            reports = {args.report: (REPORTS | SUMMARIES | BANK_REPORT)[args.report]}
        else:
            reports = REPORTS | (SUMMARIES if args.summaries and code == args.codes[0] else {})
        for report, specification in reports.items():
            if deferred is None:
                result = fetch(report, specification, code, args)
                if result["status"] == "retry_deferred":
                    deferred = result
            else:
                # 全部报表访问同一来源，限流冷却必须覆盖本次剩余公司和报表。
                result = {
                    "report": report, "code": code, "status": "not_attempted",
                    "reason": "retry_deferred", "attempts": [],
                    "blocked_by": {"code": deferred["code"], "report": deferred["report"]},
                    "next_retry_at": deferred["next_retry_at"],
                }
            checks.append(result)
            print(f"{code} {report}: {result['status']}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps({"period": args.period, "checks": checks}, ensure_ascii=False, indent=2)
    args.output.write_text(document + "\n", encoding="utf-8")
    print(f"Evidence: {args.output.resolve()}")
    return int(any(check["status"] != "ok" for check in checks))


if __name__ == "__main__":
    raise SystemExit(main())
