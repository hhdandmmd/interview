"""按固定快照与独立参考做外连接核对，不把NULL或未覆盖当作匹配。"""

import math
from collections import Counter
from decimal import Decimal

from finpipe.catalog import FIELDS


def compare_reference(left: list[dict], reference: dict) -> dict:
    """规范单位下比较并保留双侧缺键；证据不足、坏值和NULL均不算匹配。"""
    if not isinstance(reference, dict):
        raise ValueError("参考文件JSON根必须为对象")
    if type(reference.get("schema_version")) is not int or reference["schema_version"] != 1:
        raise ValueError("不支持的参考数据契约版本")
    specs = {(table, field.name): field for table, fields in FIELDS.items() for field in fields}

    def index(rows):
        if not isinstance(rows, list):
            raise ValueError("核对records必须为记录对象列表")
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("核对records每行必须为对象")
            key = tuple(
                row.get(name)
                for name in ("security_code", "report_end", "statement_type", "field_name")
            )
            if any(not isinstance(item, str) or not item for item in key):
                raise ValueError("核对业务键不能为空")
            if key[2:] not in specs:
                raise ValueError("参考字段不属于当前数据字典")
            if key in result:
                raise ValueError("核对输入含重复业务键")
            result[key] = row
        return result

    lmap, rmap = index(left), index(reference["records"])
    results = []
    for identity in sorted(set(lmap) | set(rmap)):
        lhs, rhs = lmap.get(identity), rmap.get(identity)
        entry = dict(zip(("security_code", "report_end", "statement_type", "field_name"), identity))
        entry.update(left=lhs, reference=rhs)
        if lhs is None:
            status, reason = "right_only", "参考业务键未出现在固定快照"
        elif rhs is None:
            status, reason = "left_only", "固定字段没有独立参考，不计为核验成功"
        else:
            evidence = ("reference_url", "document_sha256", "pdf_page", "report_version")
            if rhs.get("comparison_status") == "not_comparable" or any(
                not rhs.get(name) for name in evidence
            ):
                status, reason = "not_comparable", rhs.get("reason", "独立参考证据不足")
            elif lhs.get("quality_status") == "quarantined" or not lhs.get("time_valid", False):
                status, reason = "not_comparable", "记录隔离或时间证据无效，残留字段不代表可信输入"
            elif (
                lhs.get("period_basis") != specs[identity[2:]].period_basis
                or rhs.get("period_basis") != specs[identity[2:]].period_basis
            ):
                status, reason = "not_comparable", "累计、单季或时点口径未明确一致"
            elif lhs["unit"] != rhs.get("normalized_unit") or lhs.get(
                "consolidation_scope"
            ) != rhs.get("consolidation_scope"):
                status, reason = "not_comparable", "单位或合并范围不一致"
            elif lhs["value"] is None or rhs.get("reference_value") is None:
                status = (
                    "both_missing"
                    if lhs["value"] is None and rhs.get("reference_value") is None
                    else "missing_value"
                )
                reason = "NULL不是数值相等"
            elif lhs.get("field_status") not in ("ok", "warning"):
                status, reason = "not_comparable", "固定输入无效，不能只比较残留数值"
            else:
                values = [
                    lhs["value"],
                    rhs["reference_value"],
                    rhs["abs_tolerance"],
                    rhs["rel_tolerance"],
                ]
                if (
                    any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in values
                    )
                    or min(values[2:]) < 0
                ):
                    raise ValueError("核对数值及容差必须有限，容差不能为负")
                a, b, absolute, relative = (Decimal(str(value)) for value in values)
                difference = abs(a - b)
                tolerance = max(absolute, relative * max(abs(a), abs(b)))
                status = "matched" if difference <= tolerance else "mismatch"
                reason = (
                    "独立报告在声明精度内一致"
                    if status == "matched"
                    else "数值超出声明精度，需检查单位、口径和修订"
                )
                entry.update(abs_difference=float(difference), effective_tolerance=float(tolerance))
        entry.update(status=status, reason=reason)
        results.append(entry)
    counts = Counter(row["status"] for row in results)
    comparable = counts["matched"] + counts["mismatch"]
    return {
        "reference_kind": "independent_reference",
        "reference_count": len(rmap),
        "left_count": len(lmap),
        "counts": dict(counts),
        "comparison_coverage": comparable / len(rmap) if rmap else None,
        "reference_coverage": comparable / len(lmap) if lmap else None,
        "match_rate": counts["matched"] / comparable if comparable else None,
        "historical_revision_basis_verified": False,
        "rows": results,
    }
