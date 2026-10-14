"""原始响应与冻结清单：先保留字节，再验证结构，不用缓存行替代证据。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from finpipe.catalog import FIELDS, REPORT_NAMES


class ManifestError(ValueError):
    """原文摘要、业务身份或分页不满足冻结清单契约。"""


def utc_now() -> str:
    """记录真实观察时间，统一为带六位微秒的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def archive_body(directory: Path, body: bytes) -> tuple[str, str]:
    """按字节摘要归档；相同内容可复用，已有证据绝不覆盖。"""
    digest = hashlib.sha256(body).hexdigest()
    relative = f"raw/{digest}.json"
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(body)
    except FileExistsError:
        if path.read_bytes() != body:
            raise ManifestError("原文路径已存在但内容不同") from None
    return relative, digest


def read_evidence(directory: Path, evidence: dict) -> bytes:
    """仅读取清单目录内部的普通文件，拒绝目录穿越和摘要不一致。"""
    relative = Path(evidence["body_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ManifestError("原文路径必须位于清单目录内")
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ManifestError("原文文件不存在或越过清单目录")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != evidence.get("raw_hash"):
        raise ManifestError("原文 SHA256 与冻结清单不一致")
    return body


def decode_page(body: bytes, report_type: str, codes: set[str], periods: set[str]) -> dict:
    """校验结构和业务身份；数值与公告日期的语义异常留给清洗裁决。"""
    try:
        payload = json.loads(
            body, parse_float=finite_json_number, parse_constant=invalid_json_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("响应不是合法 JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ManifestError("数据源返回业务失败")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ManifestError("响应缺少 result 对象")
    rows, count, pages = result.get("data"), result.get("count"), result.get("pages")
    if (
        not isinstance(rows, list)
        or type(count) is not int
        or type(pages) is not int
        or count < 0
        or pages < 0
        or (count == 0) != (pages == 0)
    ):
        raise ManifestError("响应行数或页数非法")
    required = {"SECUCODE", "REPORT_DATE", "NOTICE_DATE"}
    required.update(field.source_field for field in FIELDS[report_type] if field.required)
    for row in rows:
        if not isinstance(row, dict) or required - row.keys():
            missing = sorted(required - row.keys()) if isinstance(row, dict) else []
            raise ManifestError(f"响应行缺少必要字段：{missing}")
        if not isinstance(row["SECUCODE"], str) or not isinstance(row["REPORT_DATE"], str):
            raise ManifestError("响应证券代码与报告日期必须为字符串")
        try:
            report_end = date.fromisoformat(str(row["REPORT_DATE"])[:10]).isoformat()
            if len(str(row["REPORT_DATE"])) > 10:
                datetime.fromisoformat(row["REPORT_DATE"])
        except ValueError as exc:
            raise ManifestError("报告日期不是合法 ISO 日期") from exc
        if row["SECUCODE"] not in codes or report_end not in periods:
            raise ManifestError("返回公司或报告期越过冻结请求范围")
    return {"rows": rows, "count": count, "pages": pages}


def finite_json_number(text: str) -> float:
    """阻止浮点溢出进入清单 JSON；原始异常字节已先行保留。"""
    number = float(text)
    if not math.isfinite(number):
        raise ManifestError("响应含超出有限浮点范围的 JSON 数字")
    return number


def invalid_json_constant(text: str) -> None:
    """JSON 协议不允许 NaN 和 Infinity，不能依赖解析器的宽松默认值。"""
    raise ManifestError(f"响应包含非法 JSON 常量：{text}")


def request_scope(
    page: dict, codes: set[str], periods: set[str], capture_kind: str
) -> tuple[set, set]:
    """由冻结查询证明页面范围；真实清单不能靠返回行反推请求已完成。"""
    requested_codes = page.get("requested_codes")
    requested_periods = page.get("requested_periods")
    if capture_kind == "real":
        query = page["request"]["params"].get("filter", "")
        code_match = re.search(r'\(SECUCODE="(\d{6}\.(?:SH|SZ|BJ))"\)', query)
        start_match = re.search(r"\(REPORT_DATE>='(\d{4}-\d{2}-\d{2})'\)", query)
        end_match = re.search(r"\(REPORT_DATE<='(\d{4}-\d{2}-\d{2})'\)", query)
        if not (code_match and start_match and end_match):
            raise ManifestError("真实清单缺少可验证的冻结查询范围")
        query_codes = {code_match.group(1)}
        query_periods = {
            period for period in periods if start_match.group(1) <= period <= end_match.group(1)
        }
        if (requested_codes and set(requested_codes) != query_codes) or (
            requested_periods and set(requested_periods) != query_periods
        ):
            raise ManifestError("页面范围元数据与冻结查询不一致")
        requested_codes, requested_periods = query_codes, query_periods
    else:
        requested_codes = requested_codes or codes
        requested_periods = requested_periods or periods
    if not requested_codes or not requested_periods:
        raise ManifestError("页面请求范围为空")
    if not set(requested_codes) <= codes or not set(requested_periods) <= periods:
        raise ManifestError("页面请求范围越过清单范围")
    return set(requested_codes), set(requested_periods)


def validate_pagination(pages: list[dict]) -> None:
    """同一请求组必须页数齐全、总数稳定，且没有分页重叠。"""
    groups: dict[str, list[dict]] = {}
    for page in pages:
        params = page["request"].get("params", {})
        group_params = {name: value for name, value in params.items() if name != "pageNumber"}
        key = page.get("group_id") or json.dumps(group_params, sort_keys=True, ensure_ascii=False)
        groups.setdefault(key, []).append(page)
    for group in groups.values():
        first = group[0]
        count, total = first["count"], first["pages"]
        numbers = [int(page["request"]["params"]["pageNumber"]) for page in group]
        # 空结果仍保留第一页失败证据，但不能作为完整财报输入。
        expected = list(range(1, max(total, 1) + 1))
        if sorted(numbers) != expected or any(
            page["count"] != count or page["pages"] != total for page in group
        ):
            raise ManifestError("分页缺失、重复或总数在请求期间发生变化")
        all_rows = [row for page in group for row in page["rows"]]
        hashes = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in all_rows]
        if len(all_rows) != count or len(hashes) != len(set(hashes)):
            raise ManifestError("分页行数与总数不符或出现完全重复行")
        for page in group:
            size = int(page["request"]["params"]["pageSize"])
            number = int(page["request"]["params"]["pageNumber"])
            if size <= 0 or len(page["rows"]) != min(size, max(0, count - (number - 1) * size)):
                raise ManifestError("分页行数不符合 pageSize 与总行数")
        if total != (count + size - 1) // size:
            raise ManifestError("页数与 pageSize 不一致")


def load_manifest(path: Path) -> dict[str, Any]:
    """重放冻结原文并重建行；缓存 rows 被忽略，观察时间保持原值。"""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ManifestError("来源清单JSON根必须为对象")
        if manifest.get("schema_version") != 1 or manifest.get("source") != "eastmoney":
            raise ManifestError("不支持的来源清单版本")
        if manifest.get("capture_kind") not in {"real", "synthetic"}:
            raise ManifestError("必须声明真实采集或合成夹具")
        if type(manifest.get("complete")) is not bool:
            raise ManifestError("清单缺少完整性标记")
        for name in ("codes", "periods"):
            values = manifest[name]
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ManifestError(f"清单{name}必须为字符串列表")
        if not isinstance(manifest["pages"], list) or any(
            not isinstance(page, dict) for page in manifest["pages"]
        ):
            raise ManifestError("清单pages必须为页面对象列表")
        codes, periods = set(manifest["codes"]), set(manifest["periods"])
        if not codes or not periods:
            raise ManifestError("清单公司和期间不能为空")
        if len(codes) != len(manifest["codes"]) or len(periods) != len(manifest["periods"]):
            raise ManifestError("清单公司或期间重复")
        page_ids = set()
        for page in manifest["pages"]:
            report_type = page["report_type"]
            if report_type not in REPORT_NAMES or page["report_name"] != REPORT_NAMES[report_type]:
                raise ManifestError("报表名与字段规格不一致")
            if page["request"]["params"]["reportName"] != page["report_name"]:
                raise ManifestError("请求报表名与页面报表名不一致")
            if page["page_id"] in page_ids:
                raise ManifestError("清单出现重复页身份")
            page_ids.add(page["page_id"])
            observed = datetime.fromisoformat(page["observed_at"].replace("Z", "+00:00"))
            if observed.utcoffset() is None:
                raise ManifestError("观察时间必须带时区")
            page_codes, page_periods = request_scope(page, codes, periods, manifest["capture_kind"])
            decoded = decode_page(
                read_evidence(path.parent, page), report_type, page_codes, page_periods
            )
            page.update(decoded)
            for attempt in page.get("attempts", []):
                if attempt.get("body_path"):
                    read_evidence(path.parent, attempt)
        for error in manifest.get("errors", []):
            if error.get("body_path"):
                read_evidence(path.parent, error)
            for attempt in error.get("attempts", []):
                if attempt.get("body_path"):
                    read_evidence(path.parent, attempt)
        if manifest["complete"]:
            if manifest.get("errors"):
                raise ManifestError("有错误的清单不能声明完整")
            validate_pagination(manifest["pages"])
            expected = {
                (code, period, kind)
                for code in codes
                for period in periods
                for kind in REPORT_NAMES
            }
            scanned = set()
            for page in manifest["pages"]:
                requested_codes, requested_periods = request_scope(
                    page,
                    codes,
                    periods,
                    manifest["capture_kind"],
                )
                scanned.update(
                    (code, period, page["report_type"])
                    for code in requested_codes
                    for period in requested_periods
                )
            if scanned != expected:
                raise ManifestError("完整清单未扫描全部计划请求范围")
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
                raise ManifestError("整批请求返回空数据，不能声明正常完成")
        return manifest
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(f"来源清单不可读取：{exc}") from exc


def save_manifest(directory: Path, manifest: dict) -> Path:
    """只创建冻结清单，拒绝覆盖已有运行；失败响应也可完整留档。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return path
