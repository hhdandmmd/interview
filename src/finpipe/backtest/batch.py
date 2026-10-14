"""简单入口的多因子编排：固定财务快照、共用行情，每项结果独立归档。"""

import html
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from finpipe.backtest.io import load_market, load_workspace_signals, run_backtest
from finpipe.backtest.market import fetch_market
from finpipe.exports import write_json
from finpipe.features.registry import FACTORS
from finpipe.timeutil import utc_now


def _save_summary(output: Path, result: dict) -> dict:
    """总览链接到各因子原报告；失败和无有效窗口均保留，不制造收益排序。"""
    result["report"] = str(output / "index.html")
    write_json(output / "batch.json", result)
    rows = []
    for item in result["results"]:
        name = item["factor"]
        link = (
            f'<a href="{html.escape(name)}/report.html">查看报告</a>'
            if item.get("report")
            else html.escape(item.get("error", "未生成报告"))
        )
        rows.append(
            f"<tr><td>{html.escape(FACTORS[name].description)}<br>{html.escape(name)}</td>"
            f"<td>{html.escape(item['status'])}</td>"
            f"<td>{item.get('summary', {}).get('formed_periods', 0)}</td><td>{link}</td></tr>"
        )
    page = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>多因子回测总览</title>'
        "<style>body{font-family:system-ui;max-width:1100px;margin:32px auto;padding:0 20px}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:12px;text-align:left}</style>"
        "<h1>多因子回测总览</h1><p>每个因子分别排序、分组和回测，未合成为组合因子。</p>"
        f"<p>状态：{html.escape(result['status'])}；财务快照：{html.escape(result['snapshot_id'])}</p>"
        f"<p>交易区间：{html.escape(result['parameters']['start'])} 至 "
        f"{html.escape(result['parameters']['end'])}；时间口径：{html.escape(result['parameters']['time_policy'])}</p>"
        "<p>observed使用本系统真实已知时间；publication_proxy为公告时间近似研究，历史修订版本未核验。</p>"
        "<p>有效窗口为0时无可评估收益；缺失报价使有关损益未知。详细口径与限制见各因子报告。</p>"
        f"<p>{html.escape(result.get('error', ''))}</p>"
        "<table><thead><tr><th>因子</th><th>状态</th><th>有效窗口</th><th>报告或错误</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table></html>"
    )
    (output / "index.html").write_text(page, encoding="utf-8")
    return result


def run_selected(
    workspace: Path, snapshot_id: str, stocks: list[str], options: dict, network: dict
) -> dict:
    """行情只获取和解析一次；某因子失败仍保存其他结果，整体返回非成功状态。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = workspace / "backtests" / (stamp + "_" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "RUNNING",
        "generated_at": utc_now(),
        "snapshot_id": snapshot_id,
        "stocks": stocks.copy(),
        "parameters": options,
        "output": str(output),
        "market_dir": options["market_dir"] or str(output / "market"),
        "results": [],
    }
    write_json(output / "batch.json", result)
    try:
        market_dir = Path(result["market_dir"])
        if options["market_dir"] is None:
            fetch_market(stocks, options["start"], options["end"], market_dir, network)
        prices, calendar, metadata = load_market(market_dir)
        missing = set(stocks) - set(metadata["codes"])
        if missing:
            raise ValueError(f"行情声明的股票池缺少配置股票：{', '.join(sorted(missing))}")
        # 本地行情可包含更多股票；研究池严格取运行前指定的stocks。
        selected = set(stocks)
        market_data = (
            [row for row in prices if row["security_code"] in selected],
            calendar,
            {**metadata, "original_codes": metadata["codes"], "codes": stocks.copy()},
        )
        parameters = {
            name: options[name] for name in ("start", "end", "horizon", "groups", "cost_bps")
        }
        for name in options["factors"]:
            try:
                signals, signal_metadata = load_workspace_signals(
                    workspace, snapshot_id, name, options["time_policy"]
                )
                item = run_backtest(
                    signals,
                    signal_metadata,
                    market_data,
                    feature=name,
                    output=output / name,
                    **parameters,
                )
            except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
                item = {"status": "FAILED", "error": str(exc), "output": str(output / name)}
            result["results"].append({"factor": name, **item})
            write_json(output / "batch.json", result)
        result["status"] = (
            "ok" if all(item["status"] == "ok" for item in result["results"]) else "incomplete"
        )
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        result.update(status="FAILED", error=str(exc))
    result["finished_at"] = utc_now()
    return _save_summary(output, result)
