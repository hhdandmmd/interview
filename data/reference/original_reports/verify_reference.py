"""复核已保存的官方报告与来源正文；人工摘录作预期，不调用业务清洗或因子代码。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

DIRECTORY = Path(__file__).resolve().parent
PROJECT = DIRECTORY.parents[2]
SOURCE_MANIFEST = PROJECT / "src/finpipe/resources/demo/manifest.json"

# 金额逐项取自已保存 PDF 的当年合并列，页码为从 1 开始的物理 PDF 页。
ANNUAL_FIELDS = {
    "600519": [
        ("income", "operating_revenue_ytd", 63, "170,899,152,276.34"),
        ("income", "operating_cost_ytd", 63, "13,789,482,367.98"),
        ("income", "net_profit_ytd", 64, "89,334,728,025.90"),
        ("income", "parent_net_profit_ytd", 64, "86,228,146,421.62"),
        ("balance", "total_assets", 59, "298,944,579,918.70"),
        ("balance", "total_liabilities", 60, "56,933,264,798.10"),
        ("balance", "total_equity", 61, "242,011,315,120.60"),
        ("balance", "current_assets", 59, "251,726,674,636.66"),
        ("balance", "current_liabilities", 60, "56,515,990,618.96"),
        ("indicator", "roe_weighted_ytd", 5, "36.02"),
        ("indicator", "source_current_ratio", None, None),
    ],
    "000333": [
        ("income", "operating_revenue_ytd", 158, "407,149,600"),
        ("income", "operating_cost_ytd", 158, "(299,584,935)"),
        ("income", "net_profit_ytd", 158, "38,757,214"),
        ("income", "parent_net_profit_ytd", 158, "38,537,237"),
        ("balance", "total_assets", 156, "604,351,853"),
        ("balance", "total_liabilities", 157, "376,684,462"),
        ("balance", "total_equity", 157, "227,667,391"),
        ("balance", "current_assets", 156, "389,063,786"),
        ("balance", "current_liabilities", 157, "351,819,806"),
        ("indicator", "roe_weighted_ytd", 9, "21.29%"),
        ("indicator", "source_current_ratio", 148, "110.59%"),
    ],
}
QUARTER_FIELDS = {
    "q1": ("2024-03-31", 8, 9, ["45,775,517,043.29", "3,381,292,339.02",
                                 "24,882,347,326.74", "24,065,262,374.15"]),
    "h1": ("2024-06-30", 30, 31, ["81,930,977,667.75", "6,750,879,251.19",
                                   "43,176,914,345.12", "41,695,610,983.37"]),
    "q3": ("2024-09-30", 8, 9, ["120,776,131,874.69", "10,228,077,394.34",
                                 "63,031,462,239.55", "60,827,552,118.51"]),
}
SOURCE_FIELDS = {
    "operating_revenue_ytd": "OPERATE_INCOME", "operating_cost_ytd": "OPERATE_COST",
    "net_profit_ytd": "NETPROFIT", "parent_net_profit_ytd": "PARENT_NETPROFIT",
    "total_assets": "TOTAL_ASSETS", "total_liabilities": "TOTAL_LIABILITIES",
    "total_equity": "TOTAL_EQUITY", "current_assets": "TOTAL_CURRENT_ASSETS",
    "current_liabilities": "TOTAL_CURRENT_LIAB", "roe_weighted_ytd": "ROEJQ",
    "source_current_ratio": "LD",
}


def main() -> int:
    """检查证据哈希、摘录实际出现及金额差异；输出新文件，绝不覆盖旧证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    metadata = json.loads((DIRECTORY / "download_manifest.json").read_text(encoding="utf-8"))
    documents = {item["report_id"]: item for item in metadata["reports"]}
    pdf_text = {}
    for report_id, item in documents.items():
        body = (DIRECTORY / item["file"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != item["sha256"]:
            raise ValueError(f"参考 PDF 摘要不符：{report_id}")
        reader = PdfReader(DIRECTORY / item["file"])
        pdf_text[report_id] = [page.extract_text() for page in reader.pages]
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    sources = {}
    for page in manifest["pages"]:
        body = (SOURCE_MANIFEST.parent / page["body_path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != page["raw_hash"]:
            raise ValueError("来源正文摘要不符")
        payload = json.loads(body, parse_float=Decimal)
        for row in payload["result"]["data"]:
            key = (row["SECUCODE"], row["REPORT_DATE"][:10], page["report_type"])
            if key in sources:
                raise ValueError("核对来源出现多版本，需要先裁决")
            sources[key] = row

    records = []

    def add_record(code: str, suffix: str, period: str, kind: str,
                   field: str, page: int | None, original: str | None) -> None:
        """把明确的单位与费用展示符号转换成独立参考，保留原值和不适用项。"""
        report_id = f"{code[:6]}_2024_{suffix}"
        document = documents[report_id]
        source_multiplier = Decimal("0.01") if field == "roe_weighted_ytd" else Decimal(1)
        source_value = Decimal(str(sources[(code, period, kind)][SOURCE_FIELDS[field]])) * source_multiplier
        unit = "percent" if field == "roe_weighted_ytd" else "cny"
        multiplier = Decimal("0.01") if unit == "percent" else Decimal(1)
        tolerance = Decimal("0.00005") if unit == "percent" else Decimal("0.01")
        reason = "原报告当年合并列；金额精度按披露单位确定。"
        if code == "000333.SZ" and kind != "indicator":
            unit, multiplier, tolerance = "cny_thousand", Decimal(1000), Decimal(500)
        if field == "source_current_ratio":
            unit, multiplier, tolerance = "percent", Decimal("0.01"), Decimal("0.00005")
            reason = "原报告流动比率 110.59% 换算为 1.1059 倍；容差取末位百分数半量化精度。"
        expected = None
        if original is not None:
            compact = "".join(pdf_text[report_id][page - 1].split())
            if original not in compact:
                raise ValueError(f"人工摘录未在指定 PDF 页出现：{report_id} {page} {original}")
            if original.startswith("("):
                if field != "operating_cost_ytd":
                    raise ValueError("本次参考不支持把非费用括号值转为正金额")
                reason = "原表“减：营业成本”括号表示利润表扣减；费用金额按正值、人民币千元×1000核对。"
            expected = Decimal(original.replace(",", "").strip("()%")) * multiplier
            difference = abs(source_value - expected)
            status = "matched" if difference <= tolerance else "mismatch"
        else:
            if any("流动比率" in "".join(text.split()) for text in pdf_text[report_id]):
                raise ValueError("原报告实际包含流动比率，不能标为未披露")
            unit, multiplier = "multiple", Decimal(1)
            reason = "茅台年报全文未直接披露流动比率；不以资产/负债复算值冒充直接披露参考。"
            status, difference = "not_comparable", None
        records.append({
            "security_code": code, "report_end": period, "statement_type": kind,
            "field_name": field, "reference_value": float(expected) if expected is not None else None,
            "source_value": float(source_value), "original_value": original, "original_unit": unit,
            "normalized_unit": ("multiple" if field == "source_current_ratio" else
                                "ratio" if field == "roe_weighted_ytd" else "cny"),
            "multiplier": float(multiplier), "abs_tolerance": float(tolerance), "rel_tolerance": 0,
            "reference_url": document["url"], "document_sha256": document["sha256"], "pdf_page": page,
            "printed_page": page - 1 if code == "000333.SZ" and page is not None else page,
            "report_version": "2024报告官网当前可下载版本，未标注修订；以保存SHA256固定",
            "comparison_status": status, "reason": reason,
            "abs_difference": float(difference) if difference is not None else None,
            "consolidation_scope": "consolidated", "reference_kind": "independent_reference",
        })

    for security, fields in ANNUAL_FIELDS.items():
        code = security + (".SH" if security == "600519" else ".SZ")
        for kind, field, page, original in fields:
            add_record(code, "annual", "2024-12-31", kind, field, page, original)
    income_fields = list(SOURCE_FIELDS)[:4]
    for suffix, (period, first_page, second_page, values) in QUARTER_FIELDS.items():
        for index, (field, value) in enumerate(zip(income_fields, values)):
            add_record("600519.SH", suffix, period, "income", field,
                       first_page if index < 2 else second_page, value)

    # 年报独立披露单季收入，预期值不由业务差分函数或本次来源累计值生成。
    quarter_values = ["45,775,517,043.29", "36,155,460,624.46", "38,845,154,206.94", "50,123,020,401.65"]
    quarterly_checks = []
    previous = Decimal(0)
    annual_document = documents["600519_2024_annual"]
    for ending, original in zip(("03-31", "06-30", "09-30", "12-31"), quarter_values):
        if original not in pdf_text["600519_2024_annual"][5]:
            raise ValueError("单季预期未出现在年报第 6 页")
        current = Decimal(str(sources[("600519.SH", "2024-" + ending, "income")]["OPERATE_INCOME"]))
        computed, expected = current - previous, Decimal(original.replace(",", ""))
        difference = abs(computed - expected)
        quarterly_checks.append({
            "security_code": "600519.SH", "report_end": "2024-" + ending,
            "field_name": "operating_revenue_q", "reference_value": float(expected),
            "source_derived_value": float(computed), "original_value": original, "original_unit": "cny",
            "multiplier": 1, "abs_tolerance": 0.01, "rel_tolerance": 0,
            "reference_url": annual_document["url"], "document_sha256": annual_document["sha256"],
            "pdf_page": 6, "comparison_status": "matched" if difference <= Decimal("0.01") else "mismatch",
            "abs_difference": float(difference), "reason": "Q1自身；Q2/Q3/Q4用同年相邻累计收入作差，预期为年报独立披露单季值。",
        })
        previous = current
    result = {"schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
              "verification_method": "人工指定原PDF字段与期间；独立Decimal核对，未调用业务清洗/因子函数",
              "source_manifest_sha256": hashlib.sha256(SOURCE_MANIFEST.read_bytes()).hexdigest(),
              "records": records, "quarterly_revenue_checks": quarterly_checks}
    counts = {status: sum(item["comparison_status"] == status for item in records)
              for status in ("matched", "not_comparable", "mismatch")}
    result["summary"] = {"planned_records": len(records), "comparable_records": len(records) - counts["not_comparable"],
                         **counts, "quarterly_revenue_matched": sum(item["comparison_status"] == "matched" for item in quarterly_checks)}
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return int(counts["mismatch"] > 0 or any(item["comparison_status"] != "matched" for item in quarterly_checks))


if __name__ == "__main__":
    raise SystemExit(main())
