"""小范围公共日线采集：保留HTTP证据，严格区分后复权报价与未复权报价。"""

import csv
import json
import math
from datetime import date
from pathlib import Path

from finpipe.backtest.calculation import CODE_PATTERN
from finpipe.exports import sha256, write_json
from finpipe.ingestion.client import SerialClient
from finpipe.timeutil import utc_now

ENDPOINT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def decode_prices(body: bytes, symbol: str, start: str, end: str, adjustment: str) -> list[dict]:
    """接口可能返回范围外640条记录；过滤范围，但达到上限且头部缺失须失败。"""
    text = body.decode("utf-8").strip()
    if not text.startswith("{") and "=" in text:
        text = text.split("=", 1)[1].strip().rstrip(";")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("行情接口正文不是合法JSON，来源结构可能发生变化") from exc
    if not isinstance(payload, dict):
        raise ValueError("行情接口正文须为JSON对象，结构可能发生变化")
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("行情接口返回业务错误")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
        raise ValueError("行情data及对应证券须为对象，结构可能发生变化")
    item = data[symbol]
    field = "hfqday" if adjustment == "hfq" else "day"
    rows = item.get(field)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"行情缺少{field}，不能用其他复权口径替代")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            raise ValueError("行情列结构发生变化")
        if not isinstance(row[0], str):
            raise ValueError("行情交易日期须为ISO日期字符串")
        day = date.fromisoformat(row[0]).isoformat()
        if day in seen:
            raise ValueError("同一响应内交易日期重复")
        seen.add(day)
        if isinstance(row[2], bool) or not isinstance(row[2], (str, int, float)):
            raise ValueError("行情收盘价须为数值或数值字符串")
        try:
            close = float(row[2])
        except (ValueError, OverflowError) as exc:
            raise ValueError("行情收盘价须为有限正数") from exc
        if not math.isfinite(close) or close <= 0:
            raise ValueError("行情收盘价不是有限正数")
        if start <= day <= end:
            result.append({"trade_date": day, "close": close})
    if len(rows) >= 640 and min(seen) > start:
        raise ValueError("行情达到返回条数上限，起始区间可能被截断")
    return sorted(result, key=lambda row: row["trade_date"])


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """固定列与UTF-8 BOM便于Windows直接打开，NULL输出空单元格。"""
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fetch_market(codes: list[str], start: str, end: str, output: Path, network: dict) -> dict:
    """按证券和年分块采集，最多50只、五年；失败保存证据并禁止消费半成品。"""
    start, end = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
    if start > end or int(end[:4]) - int(start[:4]) > 4:
        raise ValueError("行情范围须为正向且至多跨五个自然年")
    if not codes or len(codes) > 50 or len(set(codes)) != len(codes):
        raise ValueError("行情证券须为1到50只且不重复")
    if any(not CODE_PATTERN.fullmatch(code) or code.endswith(".BJ") for code in codes):
        raise ValueError("本行情入口支持SH/SZ；BJ请导入外部同口径行情")
    if "000001.SH" in codes:
        raise ValueError("000001.SH为日历代理指数，不能作为股票样本")
    output = output.resolve()
    if output.exists():
        raise ValueError("行情输出目录已存在，请使用新的目录，保留已有采集证据")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "kind": "public_market",
        "source": "tencent",
        "adjustment": "hfq",
        "codes": codes,
        "start": start,
        "end": end,
        "complete": False,
        "calendar_kind": "sh000001_observed_sessions",
        "requests": [],
        "files": {},
        "notice": "上证指数报价日期作为交易日代理；后复权日线不含可交易性核验，不保证含税分红总收益。",
    }
    price_map, calendar, client = {}, set(), None
    try:
        client = SerialClient({**network, "endpoint": ENDPOINT}, output)
        client.session.headers["Referer"] = "https://gu.qq.com/"
        for code in ["000001.SH", *codes]:
            is_calendar = code == "000001.SH"
            symbol = code[-2:].lower() + code[:6]
            for year in range(int(start[:4]), int(end[:4]) + 1):
                left, right = max(start, f"{year}-01-01"), min(end, f"{year}-12-31")
                adjustment = "none" if is_calendar else "hfq"
                param = f"{symbol},day,{left},{right},640,{'' if is_calendar else 'hfq'}"
                body, evidence = client.get({"param": param})
                manifest["requests"].append(
                    {**evidence, "security_code": code, "adjustment": adjustment}
                )
                if body is None:
                    raise ValueError("行情请求失败，请查看manifest.json中的请求上下文")
                rows = decode_prices(body, symbol, left, right, adjustment)
                if is_calendar:
                    calendar.update(row["trade_date"] for row in rows)
                else:
                    for row in rows:
                        key = (code, row["trade_date"])
                        if key in price_map and price_map[key] != row["close"]:
                            raise ValueError("重叠响应的价格不一致，不能拼接不同版本")
                        price_map[key] = row["close"]
        if not calendar or not price_map:
            raise ValueError("行情或日历为空")
        if any(day not in calendar for _, day in price_map):
            raise ValueError("个股报价日未在指数日历出现，请核查来源完整性")
        write_csv(
            output / "prices.csv",
            [
                {"security_code": code, "trade_date": day, "close": close}
                for (code, day), close in sorted(price_map.items())
            ],
            ["security_code", "trade_date", "close"],
        )
        write_csv(
            output / "calendar.csv",
            [{"trade_date": day} for day in sorted(calendar)],
            ["trade_date"],
        )
        manifest["files"] = {name: sha256(output / name) for name in ("prices.csv", "calendar.csv")}
        manifest.update(complete=True, price_rows=len(price_map), calendar_sessions=len(calendar))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise ValueError(f"行情采集未完成：{exc}；证据目录：{output}") from exc
    finally:
        manifest["captured_at"] = utc_now()
        try:
            write_json(output / "manifest.json", manifest)
        finally:
            if client is not None:
                client.close()
    return {
        "status": "ok",
        "output": str(output),
        "price_rows": len(price_map),
        "calendar_sessions": len(calendar),
    }
