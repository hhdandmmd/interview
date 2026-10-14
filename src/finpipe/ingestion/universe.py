"""用真实年报列表冻结非金融样本；行业缺失不凭公司名称猜测资格。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .archive import (
    ManifestError,
    finite_json_number,
    invalid_json_constant,
    utc_now,
    validate_pagination,
)
from .client import SerialClient

UNIVERSE_REPORT = "RPT_LICO_FN_CPD"
UNIVERSE_PERIOD = "2025-12-31"
FINANCIAL_INDUSTRIES = {"银行", "证券", "保险", "多元金融", "非银金融", "金融服务"}


def is_financial(industry: str) -> bool:
    """来源行业末尾层级数字不改变分类；只匹配明确金融类别。"""
    normalized = re.sub(r"[ⅠⅡⅢⅣⅤIV12345]+$", "", industry.strip())
    return normalized in FINANCIAL_INDUSTRIES


def choose_companies(companies: list[dict], sample_size: int) -> list[dict]:
    """沪深京按约 4:4:2 确定性分层；不足部分透明转给有余量的市场。"""
    by_market = {
        market: sorted(
            (item for item in companies if item["exchange"] == market),
            key=lambda item: item["security_code"],
        )
        for market in ("SH", "SZ", "BJ")
    }
    targets = {"SH": sample_size * 2 // 5, "SZ": sample_size * 2 // 5, "BJ": sample_size // 5}
    for market in ("SH", "SZ", "BJ"):
        if sum(targets.values()) < sample_size:
            targets[market] += 1
    if sample_size >= 3:
        for market in targets:
            if not targets[market] and by_market[market]:
                donor = max(targets, key=targets.get)
                targets[donor] -= 1
                targets[market] = 1
    selected = []
    for market in by_market:
        selected.extend(by_market[market][: targets[market]])
    selected_codes = {item["security_code"] for item in selected}
    remaining = sorted(
        (item for item in companies if item["security_code"] not in selected_codes),
        key=lambda item: (item["exchange"], item["security_code"]),
    )
    selected.extend(remaining[: sample_size - len(selected)])
    return sorted(selected, key=lambda item: item["security_code"])


def select_universe(sample_size: int, settings: dict, archive_dir: Path) -> dict:
    """完整分页读取 2025 年报 A 股名单，冻结分类证据与入选公司。"""
    if type(sample_size) is not int or sample_size < 1:
        raise ValueError("样本数量必须为正整数")
    output = archive_dir / "universe.json"
    if output.exists():
        raise FileExistsError("冻结公司名单已存在，请使用新的目录")
    document = {
        "schema_version": 1,
        "source": "eastmoney",
        "report_end": UNIVERSE_PERIOD,
        "selection_policy": "nonfinancial_board_market_4_4_2_code_sorted_v1",
        "requested_sample_size": sample_size,
        "captured_at": utc_now(),
        "capture_kind": "real",
        "codes": [],
        "companies": [],
        "pages": [],
        "complete": False,
        "errors": [],
        "excluded_financial": 0,
        "unknown_industry": 0,
    }
    client = SerialClient(settings, archive_dir)
    number, total = 1, 1
    try:
        while number <= total:
            params = {
                "reportName": UNIVERSE_REPORT,
                "columns": "ALL",
                "pageNumber": str(number),
                "pageSize": "500",
                "sortColumns": "SECURITY_CODE",
                "sortTypes": "1",
                "filter": f"(REPORTDATE='{UNIVERSE_PERIOD}')(SECURITY_TYPE_CODE=\"058001001\")",
                "source": "WEB",
                "client": "WEB",
            }
            body, evidence = client.get(params)
            if body is None:
                document["errors"].append(evidence)
                break
            try:
                payload = json.loads(
                    body, parse_float=finite_json_number, parse_constant=invalid_json_constant
                )
                result = payload.get("result")
                if payload.get("success") is not True or not isinstance(result, dict):
                    raise ManifestError("年报列表请求未成功")
                rows, count, pages = result.get("data"), result.get("count"), result.get("pages")
                if (
                    not isinstance(rows, list)
                    or type(count) is not int
                    or type(pages) is not int
                    or count <= 0
                    or pages <= 0
                    or pages > 100
                ):
                    raise ManifestError("年报列表行数或页数异常")
                for row in rows:
                    required = {
                        "SECUCODE",
                        "SECURITY_NAME_ABBR",
                        "REPORTDATE",
                        "SECURITY_TYPE_CODE",
                        "BOARD_NAME",
                        "BOARD_CODE",
                    }
                    if not isinstance(row, dict) or required - row.keys():
                        raise ManifestError("年报列表缺少分类字段")
                    if (
                        re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row["SECUCODE"])) is None
                        or str(row["REPORTDATE"])[:10] != UNIVERSE_PERIOD
                        or row["SECURITY_TYPE_CODE"] != "058001001"
                    ):
                        raise ManifestError("年报列表返回了范围外证券或报告期")
                document["pages"].append({**evidence, "rows": rows, "count": count, "pages": pages})
                total = pages
            except (ValueError, AttributeError, ManifestError) as exc:
                document["errors"].append({**evidence, "status": "schema_error", "error": str(exc)})
                break
            number += 1
        if not document["errors"]:
            try:
                validate_pagination(document["pages"])
                companies = {}
                for page in document["pages"]:
                    for row in page["rows"]:
                        code, industry = row["SECUCODE"], row["BOARD_NAME"]
                        if not isinstance(industry, str) or not industry.strip():
                            document["unknown_industry"] += 1
                            continue
                        if is_financial(industry):
                            document["excluded_financial"] += 1
                            continue
                        if code in companies:
                            raise ManifestError("年报名单出现重复公司，需先裁决来源版本")
                        companies[code] = {
                            "security_code": code,
                            "name": row["SECURITY_NAME_ABBR"],
                            "exchange": code.rsplit(".", 1)[1],
                            "industry": industry,
                            "industry_code": row["BOARD_CODE"],
                            "org_type": row.get("ORG_TYPE"),
                            "eligibility": "eligible",
                            "historical_eligibility": "unknown",
                            "qualification_evidence": {
                                "report_name": UNIVERSE_REPORT,
                                "report_end": UNIVERSE_PERIOD,
                                "industry_field": "BOARD_NAME",
                                "raw_hash": page["raw_hash"],
                                "body_path": page["body_path"],
                                "observed_at": page["observed_at"],
                            },
                        }
                if len(companies) < sample_size:
                    raise ManifestError(f"有明确非金融分类证据的公司不足 {sample_size} 家")
                document["companies"] = choose_companies(list(companies.values()), sample_size)
                document["codes"] = [item["security_code"] for item in document["companies"]]
                document["complete"] = True
                document["available_nonfinancial"] = len(companies)
            except ManifestError as exc:
                document["errors"].append({"status": "universe_error", "error": str(exc)})
    finally:
        client.close()
    document["captured_at"] = utc_now()
    archive_dir.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return document
