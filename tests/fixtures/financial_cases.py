"""独立手算夹具：人工累计报表与目标分数分开维护，不调用生产计算代码。"""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

PERIODS = (
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
)
NOTICES = (
    "2023-05-10",
    "2023-08-15",
    "2023-11-15",
    "2024-04-15",
    "2024-05-10",
    "2024-08-15",
    "2024-11-15",
    "2025-04-15",
)
CAPTURED_AT = "2025-05-01T08:00:00.000000Z"
REPORT_NAMES = {
    "income": "RPT_F10_FINANCE_GINCOME",
    "balance": "RPT_F10_FINANCE_GBALANCE",
    "indicator": "RPT_F10_FINANCE_MAINFINADATA",
}

# 金额采用人民币元；累计数手工列出，便于发现跨年或滚动窗口误差。
COMPANIES = {
    "600519.SH": {
        "name": "人工夹具甲公司",
        "revenue": (100, 220, 360, 520, 150, 348, 600, 912),
        "cost": (60, 126, 196, 268, 84, 186, 312, 456),
        "profit": (10, 28, 56, 96, 24, 60, 114, 192),
        "parent_profit": (8, 23, 47, 82, 20, 50, 95, 161),
        "assets": (1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700),
        "liabilities": (300, 352, 408, 468, 532, 600, 672, 748),
        "current_assets": (300, 330, 360, 390, 420, 480, 540, 600),
        "current_liabilities": (100, 110, 120, 130, 140, 150, 180, 200),
        "roe_percent": (2, 5, 9, 14, 3, 7.5, 12.6, 19.6),
    },
    "000333.SZ": {
        "name": "人工夹具乙公司",
        "revenue": (80, 180, 300, 440, 100, 230, 400, 620),
        "cost": (48, 110, 182, 266, 55, 133, 218, 328),
        "profit": (8, 20, 38, 59, 15, 41, 75, 119),
        "parent_profit": (6, 16, 31, 49, 12, 32, 60, 100),
        "assets": (500, 550, 600, 650, 700, 750, 800, 850),
        "liabilities": (200, 220, 240, 260, 280, 300, 320, 340),
        "current_assets": (150, 165, 180, 195, 210, 225, 240, 255),
        "current_liabilities": (75, 75, 80, 100, 100, 125, 120, 150),
        "roe_percent": (1, 3, 6, 10, 1.5, 4.5, 9, 15),
    },
}

# 以下分数来自独立手算；列顺序是2024Q1至Q4，不能由待测函数生成。
EXPECTED = {
    "600519.SH": {
        "roe_weighted_ytd": ("3/100", "3/40", "63/500", "49/250"),
        "roe_change_yoy": ("1/2", "1/2", "2/5", "2/5"),
        "roe_change_yoy_pp": ("1/100", "1/40", "9/250", "7/125"),
        "net_margin_q": ("4/25", "2/11", "3/14", "1/4"),
        "gross_margin_q": ("11/25", "16/33", "1/2", "7/13"),
        "net_margin_ytd": ("4/25", "5/29", "19/100", "4/19"),
        "gross_margin_ytd": ("11/25", "27/58", "12/25", "1/2"),
        "revenue_growth_yoy": ("1/2", "13/20", "4/5", "19/20"),
        "revenue_growth_qoq": ("-1/16", "8/25", "3/11", "5/21"),
        "parent_profit_growth_yoy": ("3/2", "1", "7/8", "31/35"),
        "parent_profit_growth_qoq": ("-3/7", "1/2", "1/2", "7/15"),
        "debt_asset_ratio": ("19/50", "2/5", "21/50", "11/25"),
        "debt_asset_change_qoq_pp": ("1/50", "1/50", "1/50", "1/50"),
        "current_ratio": ("3", "16/5", "3", "3"),
        "current_ratio_change_qoq": ("0", "1/5", "-1/5", "0"),
    },
    "000333.SZ": {
        "roe_weighted_ytd": ("3/200", "9/200", "9/100", "3/20"),
        "roe_change_yoy": ("1/2", "1/2", "1/2", "1/2"),
        "roe_change_yoy_pp": ("1/200", "3/200", "3/100", "1/20"),
        "net_margin_q": ("3/20", "1/5", "1/5", "1/5"),
        "gross_margin_q": ("9/20", "2/5", "1/2", "1/2"),
        "net_margin_ytd": ("3/20", "41/230", "3/16", "119/620"),
        "gross_margin_ytd": ("9/20", "97/230", "91/200", "73/155"),
        "revenue_growth_yoy": ("1/4", "3/10", "5/12", "4/7"),
        "revenue_growth_qoq": ("-2/7", "3/10", "4/13", "5/17"),
        "parent_profit_growth_yoy": ("1", "1", "13/15", "11/9"),
        "parent_profit_growth_qoq": ("-1/3", "2/3", "2/5", "3/7"),
        "debt_asset_ratio": ("2/5", "2/5", "2/5", "2/5"),
        "debt_asset_change_qoq_pp": ("0", "0", "0", "0"),
        "current_ratio": ("21/10", "9/5", "2", "17/10"),
        "current_ratio_change_qoq": ("3/20", "-3/10", "1/5", "-3/10"),
    },
}


def expected_rows() -> list[dict]:
    """返回120个独立预期值；保留原分数供人工复核。"""
    return [
        {
            "security_code": code,
            "report_end": period,
            "feature_name": name,
            "feature_value": float(Fraction(fractions[index])),
            "exact_fraction": fractions[index],
        }
        for code, features in EXPECTED.items()
        for name, fractions in features.items()
        for index, period in enumerate(PERIODS[4:])
    ]


def source_rows(report_type: str) -> list[dict]:
    """构造明确标识人工的来源行；指标用于同口径核对，目标值另存。"""
    rows = []
    for code, values in COMPANIES.items():
        for index, period in enumerate(PERIODS):
            row = {
                "SECUCODE": code,
                "SECURITY_CODE": code[:6],
                "SECURITY_NAME_ABBR": values["name"],
                "REPORT_DATE": period + " 00:00:00",
                "NOTICE_DATE": NOTICES[index] + " 00:00:00",
                "UPDATE_DATE": NOTICES[index] + " 00:00:00",
                "ORG_TYPE": "通用",
                "ORG_CODE": "synthetic-" + code,
                "CURRENCY": "CNY",
                "_revision_sequence": 1,
                "_evidence": {
                    "authoritative_revision": True,
                    "capture_kind": "synthetic",
                    "consolidation_scope": "consolidated",
                },
            }
            if report_type == "income":
                row.update(
                    OPERATE_INCOME=values["revenue"][index],
                    TOTAL_OPERATE_INCOME=values["revenue"][index],
                    OPERATE_COST=values["cost"][index],
                    NETPROFIT=values["profit"][index],
                    PARENT_NETPROFIT=values["parent_profit"][index],
                )
            elif report_type == "balance":
                equity = values["assets"][index] - values["liabilities"][index]
                row.update(
                    TOTAL_ASSETS=values["assets"][index],
                    TOTAL_LIABILITIES=values["liabilities"][index],
                    TOTAL_EQUITY=equity,
                    TOTAL_PARENT_EQUITY=equity,
                    TOTAL_CURRENT_ASSETS=values["current_assets"][index],
                    TOTAL_CURRENT_LIAB=values["current_liabilities"][index],
                )
            elif report_type == "indicator":
                row.update(
                    ROEJQ=values["roe_percent"][index],
                    XSMLL=100
                    * (values["revenue"][index] - values["cost"][index])
                    / values["revenue"][index],
                    XSJLL=100 * values["profit"][index] / values["revenue"][index],
                    ZCFZL=100 * values["liabilities"][index] / values["assets"][index],
                    LD=values["current_assets"][index] / values["current_liabilities"][index],
                )
            else:
                raise ValueError("未知夹具报表类型")
            rows.append(row)
    return rows


def write_manifest(
    directory: Path, mutate=None, observed_at: str = CAPTURED_AT, periods=PERIODS
) -> Path:
    """向独立目录写合成原文与摘要；变体不会改动仓库或真实来源证据。"""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "source": "eastmoney",
        "codes": list(COMPANIES),
        "periods": list(periods),
        "captured_at": observed_at,
        "capture_kind": "synthetic",
        "synthetic_notice": "仅用于测试，金额及公司名为人工构造，不能作为真实公司数据。",
        "pages": [],
        "complete": True,
        "errors": [],
    }
    for report_type, report_name in REPORT_NAMES.items():
        rows = [row for row in source_rows(report_type) if row["REPORT_DATE"][:10] in periods]
        if mutate is not None:
            mutate(report_type, rows)
        body = {
            "success": True,
            "code": 0,
            "result": {"pages": 1, "count": len(rows), "data": rows},
        }
        content = json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        body_path = f"{report_type}.json"
        (directory / body_path).write_bytes(content)
        manifest["pages"].append(
            {
                "page_id": f"synthetic-{report_type}-{observed_at}",
                "body_path": body_path,
                "raw_hash": hashlib.sha256(content).hexdigest(),
                "report_type": report_type,
                "report_name": report_name,
                "observed_at": observed_at,
                "capture_kind": "synthetic",
                "request": {
                    "params": {
                        "reportName": report_name,
                        "pageNumber": "1",
                        "pageSize": str(len(rows)),
                    }
                },
            }
        )
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    target = Path(__file__).parent / "synthetic_two_companies"
    write_manifest(target)
    (target / "expected.json").write_text(
        json.dumps(expected_rows(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
