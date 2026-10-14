"""将已有分组结果呈现为中文报告和日期净值图，不修改信号或收益计算。"""

import html
import json
import math
from datetime import date

from finpipe.features.specifications import feature_dictionary

COLORS = ("#64748b", "#0f766e", "#d97706", "#7c3aed", "#dc2626")
REFERENCE = "同截面等权参考"


def _esc(value) -> str:
    return html.escape(str(value))


def _group_label(group: int, groups: int) -> str:
    suffix = " · 因子最低" if group == 1 else " · 因子最高" if group == groups else ""
    return f"G{group}{suffix}"


def _color(group: int) -> str:
    return COLORS[group - 1] if group <= len(COLORS) else f"hsl({group * 137.5 % 360:.0f} 65% 40%)"


def _percent(value: float | None) -> str:
    return "缺失" if value is None else f"{value:.2%}"


def _number(value: float | None) -> str:
    return "缺失" if value is None else f"{value:.4f}"


def _factor_value(value: float, unit: str) -> str:
    if unit == "ratio":
        return _percent(value)
    if unit == "ratio_delta":
        return f"{value * 100:.2f} 个百分点"
    if unit in ("multiple", "multiple_delta"):
        return f"{value:.4f} 倍" + ("差" if unit == "multiple_delta" else "")
    return f"{value:.4f}"


def _feature_spec(feature: str) -> dict:
    return next((item for item in feature_dictionary() if item["name"] == feature), {})


def _timing_label(report: dict) -> str:
    if report["market_metadata"].get("adjustment") == "synthetic":
        return "人工样例，仅验证计算"
    if report["signal_metadata"]["time_policy"] == "publication_proxy":
        return "公告时间近似研究，历史版本未核验"
    return "实际观察与发布可用时间"


def chart_data(report: dict) -> list[dict]:
    """首个入场日从1开始；参考采用全部有效成员等权，并扣除与分组相同的费用。

    cohort_return来自个股等权收益，不能用各组收益平均替代：组人数可能不同。
    任一参考收益未知后，参考累计净值持续为NULL；不跨缺失区间恢复净值。
    """
    start = report["summary"]["first_entry_date"]
    if start is None:
        return []
    groups = report["parameters"]["groups"]
    rows = [{"trade_date": start, "series": f"G{i}", "nav": 1.0} for i in range(1, groups + 1)]
    rows.append({"trade_date": start, "series": REFERENCE, "nav": 1.0})
    rows.extend(
        {"trade_date": row["exit_date"], "series": f"G{row['group']}", "nav": row["nav"]}
        for row in report["nav"]
    )
    windows = {row["exit_date"]: row for row in report["period_returns"]}
    fee = report["parameters"]["cost_bps"] / 10000
    reference = 1.0
    for day, row in sorted(windows.items()):
        gross = row["cohort_return"]
        reference = (
            reference * (1 - fee) ** 2 * (1 + gross)
            if reference is not None and gross is not None
            else None
        )
        rows.append({"trade_date": day, "series": REFERENCE, "nav": reference})
    return sorted(rows, key=lambda row: (row["trade_date"], row["series"]))


def _y_scale(values: list[float]) -> tuple[float, float, list[float], int]:
    """给常数净值留出空间，选择可读刻度并保证1.0处于绘图区。"""
    low, high = min(values), max(values)
    padding = max((high - low) * 0.08, 0.03)
    low, high = max(0, low - padding), high + padding
    raw = (high - low) / 5
    power = 10 ** math.floor(math.log10(raw))
    step = next(
        multiplier * power for multiplier in (1, 2, 2.5, 5, 10) if multiplier * power >= raw
    )
    low, high = math.floor(low / step) * step, math.ceil(high / step) * step
    ticks = [low + index * step for index in range(round((high - low) / step) + 1)]
    decimals = max(2, 1 - math.floor(math.log10(step)))
    return low, high, ticks, decimals


def nav_chart(report: dict) -> str:
    """独立SVG使用真实日期间距、净值坐标和点提示；缺值使曲线停止，空结果不画收益。"""
    rows = chart_data(report)
    days = sorted({row["trade_date"] for row in rows}) or [
        report["summary"]["calendar_first_session"],
        report["summary"]["calendar_last_session"],
    ]
    first, last = date.fromisoformat(days[0]).toordinal(), date.fromisoformat(days[-1]).toordinal()
    low, high, ticks, decimals = _y_scale(
        [1.0, *(row["nav"] for row in rows if row["nav"] is not None)]
    )
    groups = report["parameters"]["groups"]
    legend = [
        (_group_label(group, groups), _color(group), False) for group in range(1, groups + 1)
    ] + [(REFERENCE, "#334155", True), ("基准线 1.0", "#94a3b8", True)]
    left, right, top = 90, 1160, 48 + math.ceil(len(legend) / 4) * 25
    bottom = top + 400

    def x(day):
        return left + (date.fromisoformat(day).toordinal() - first) / max(1, last - first) * (
            right - left
        )

    def y(value):
        return bottom - (value - low) / (high - low) * (bottom - top)

    name = _feature_spec(report["parameters"]["feature"]).get(
        "description", report["parameters"]["feature"]
    )
    title = f"{name} · 分组净值与等权参考"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 {bottom + 135}" role="img" '
        f'aria-label="{_esc(title)}" style="font-family:Microsoft YaHei,Arial,sans-serif;font-size:16px">',
        f"<title>{_esc(title)}</title><desc>横轴为实际日期，纵轴为初始值1的累计净值；仅观察持有窗口端点。</desc>",
        f'<rect width="1200" height="{bottom + 135}" fill="white"/>',
        f'<text x="{left}" y="24" font-size="20" font-weight="600">{_esc(title)}</text>',
    ]
    for index, (label, color, dashed) in enumerate(legend):
        lx, ly = left + index % 4 * 270, 50 + index // 4 * 25
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        parts.append(
            f'<line x1="{lx}" x2="{lx + 28}" y1="{ly}" y2="{ly}" stroke="{color}" stroke-width="2.4"{dash}/>'
            f'<text x="{lx + 36}" y="{ly + 5}" font-size="15">{_esc(label)}</text>'
        )
    for tick in ticks:
        position = y(tick)
        parts.append(
            f'<line x1="{left}" x2="{right}" y1="{position:.2f}" y2="{position:.2f}" stroke="#e2e8f0"/>'
            f'<text x="{left - 14}" y="{position + 5:.2f}" text-anchor="end" fill="#475569">{tick:.{decimals}f}</text>'
        )
    tick_count = min(6, len(days))
    tick_days = [
        days[round(index * (len(days) - 1) / max(1, tick_count - 1))] for index in range(tick_count)
    ]
    for day in tick_days:
        position = x(day)
        anchor = "end" if day == days[-1] else "middle"
        parts.append(
            f'<line x1="{position:.2f}" x2="{position:.2f}" y1="{top}" y2="{bottom}" stroke="#f1f5f9"/>'
            f'<text x="{position:.2f}" y="{bottom + 32}" text-anchor="{anchor}" fill="#475569">{day}</text>'
        )
    parts.extend(
        [
            f'<path d="M {left} {top} V {bottom} H {right}" fill="none" stroke="#475569" stroke-width="1.3"/>',
            f'<text x="26" y="{(top + bottom) / 2}" text-anchor="middle" transform="rotate(-90 26 {(top + bottom) / 2})">净值（初始值 = 1）</text>',
            f'<text x="625" y="{bottom + 68}" text-anchor="middle">日期（入场日及持有窗口结束日）</text>',
            f'<text x="{left}" y="{bottom + 100}" font-size="14" fill="#64748b">窗口端点净值；同截面等权参考按同一费用扣费，不代表市场指数。</text>',
            f'<text x="{left}" y="{bottom + 125}" font-size="14" fill="#64748b">{_esc(_timing_label(report))} · 因子代码：{_esc(report["parameters"]["feature"])}</text>',
            f'<line x1="{left}" x2="{right}" y1="{y(1):.2f}" y2="{y(1):.2f}" stroke="#94a3b8" stroke-dasharray="5 5"/>',
        ]
    )
    for series in [*(f"G{i}" for i in range(1, groups + 1)), REFERENCE]:
        color = "#334155" if series == REFERENCE else _color(int(series[1:]))
        points = []
        marks = []
        for row in (item for item in rows if item["series"] == series):
            if row["nav"] is None:
                break
            px, py = x(row["trade_date"]), y(row["nav"])
            points.append(f"{px:.2f},{py:.2f}")
            marks.append(
                f'<circle cx="{px:.2f}" cy="{py:.2f}" r="3" fill="{color}">'
                f"<title>{_esc(series)} | {row['trade_date']} | 净值 {row['nav']:.4f}</title></circle>"
            )
        dash = ' stroke-dasharray="9 5"' if series == REFERENCE else ""
        parts.append(
            f'<g data-series="{_esc(series)}"><polyline points="{" ".join(points)}" fill="none" '
            f'stroke="{color}" stroke-width="2.4"{dash}/>' + "".join(marks) + "</g>"
        )
    if not rows:
        parts.append(
            f'<text x="625" y="{top + 160}" text-anchor="middle" fill="#64748b">未形成有效持有窗口，暂无可绘制净值</text>'
        )
    return "".join(parts) + "</svg>"


def render_report(report: dict, chart: str) -> str:
    """因子定义复用财务字典，首屏说明分组、时间口径和真实覆盖；收益展示为百分数。"""
    params, summary = report["parameters"], report["summary"]
    feature = params["feature"]
    spec = _feature_spec(feature)
    name, unit = spec.get("description", "外部因子"), spec.get("storage_unit", "unknown")
    groups = params["groups"]
    timing = _timing_label(report)
    first, last = summary["first_entry_date"], summary["last_exit_date"]
    period = f"{first} 至 {last}" if first else "未形成有效窗口"
    status = "完成" if report["status"] == "ok" else "数据不完整"
    explanation = (
        "本次使用公司披露的年初至报告期末累计加权净资产收益率，按百分数展示。它不是ROE同比变化率，也不是单季度ROE。"
        if feature == "roe_weighted_ytd"
        else f"计算公式：{spec.get('formula', '由外部信号提供者定义')}。因子单位：{spec.get('display_unit', '按外部原值展示')}。"
    )
    table_rows = "".join(
        f"<tr><td>{_group_label(row['group'], groups)}</td>"
        f"<td>{_number(None if row['cumulative_return'] is None else 1 + row['cumulative_return'])}</td>"
        f"<td>{_percent(row['cumulative_return'])}</td><td>{_percent(row['max_drawdown_at_period_ends'])}</td></tr>"
        for row in summary["groups"]
    )
    reference = [row["nav"] for row in chart_data(report) if row["series"] == REFERENCE]
    if reference and reference[-1] is not None:
        peak, drawdown = 1.0, 0.0
        for value in reference:
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        table_rows += f"<tr><td>{REFERENCE}</td><td>{reference[-1]:.4f}</td><td>{_percent(reference[-1] - 1)}</td><td>{_percent(drawdown)}</td></tr>"
    else:
        table_rows += f"<tr><td>{REFERENCE}</td><td>缺失</td><td>缺失</td><td>缺失</td></tr>"
    first_members = [row for row in report["memberships"] if row["entry_date"] == first]
    member_rows = (
        "".join(
            f"<tr><td>G{row['group']}</td><td>{_esc(row['security_code'])}</td><td>{_esc(row['report_end'])}</td>"
            f"<td>{_factor_value(row['feature_value'], unit)}</td><td>{_percent(row['weight'])}</td></tr>"
            for row in first_members[:20]
        )
        or '<tr><td colspan="5">暂无有效分组成员，具体原因见覆盖情况与排除明细。</td></tr>'
    )
    notices = [
        report["signal_metadata"].get("notice", "外部信号时间口径由提交者声明，未独立核验。"),
        report["market_metadata"].get("notice", "外部行情口径由提交者声明。"),
        *report["limitations"],
    ]
    skipped = summary["skipped_decisions"].get("insufficient_factor_members", 0)
    details = _esc(
        json.dumps({"parameters": params, "summary": summary}, ensure_ascii=False, indent=2)
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(name)} · 分组回测</title><style>"
        'body{max-width:1200px;margin:32px auto;padding:0 24px;font:16px/1.7 "Microsoft YaHei",Arial,sans-serif;color:#1e293b;background:#f8fafc}'
        "h1{margin-bottom:4px}h2{font-size:22px;margin:0 0 12px}h3{font-size:17px}p{margin:8px 0}"
        "section{background:white;border:1px solid #e2e8f0;border-radius:12px;padding:24px;margin:20px 0}"
        ".muted{color:#64748b}.notice{background:#fff7ed;border-left:4px solid #d97706;padding:12px 16px;margin:18px 0}"
        ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.card{background:white;border:1px solid #e2e8f0;border-radius:8px;padding:16px}.card b{display:block;font-size:18px}"
        ".chart{overflow-x:auto}.chart svg{width:100%;min-width:720px;display:block}.table{overflow-x:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{border-bottom:1px solid #e2e8f0;padding:10px;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#f1f5f9}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#0369a1}code{overflow-wrap:anywhere}summary{cursor:pointer;font-weight:bold}</style></head><body>"
        f'<h1>{_esc(name)} · 分组回测</h1><p class="muted">因子标识：<code>{_esc(feature)}</code> · {_esc(timing)} · 状态：{status}</p>'
        f'<div class="notice">{_esc(explanation)} 每个决策日按当时可用的最新因子值从低到高分{groups}组，G1最低，G{groups}最高；组内等权，每期重新分组。</div>'
        '<div class="cards">'
        f'<div class="card"><span class="muted">研究股票池</span><b>{summary["universe_size"]}家公司 / {groups}组</b></div>'
        f'<div class="card"><span class="muted">实际形成持有窗口</span><b>{summary["formed_periods"]}期</b></div>'
        f'<div class="card"><span class="muted">持有期与进入方式</span><b>{params["horizon"]}个交易日 / 次日收盘</b></div>'
        f'<div class="card"><span class="muted">单边费用</span><b>{params["cost_bps"]:g}个基点</b></div></div>'
        f"<section><h2>分组净值与等权参考</h2><p>实际持有范围：<b>{period}</b>。横轴为日期，纵轴为净值；1.20表示累计收益20%。</p>"
        f'<div class="chart">{chart}</div>'
        f'<p class="muted">{REFERENCE}使用每期全部有效因子成员的个股等权收益，费用与分组相同；它不是市场指数。仅绘制入场初始值和持有窗口端点，连线不代表逐日净值。悬停圆点可查看日期和数值。</p>'
        f'<p class="muted">请求范围：{params["start"]} 至 {params["end"]}；因成员不足跳过{skipped}次决策，前置现金期不绘制为有效回测。</p>'
        '<p><a href="nav.svg">打开或保存独立SVG图</a> · <a href="chart_data.csv">图表数据CSV</a></p></section>'
        '<section><h2>分组结果</h2><p class="muted">看各组表现是否随因子值有规律地变化；高因子组不一定收益更高。回撤只在持有窗口端点计算。</p>'
        '<div class="table"><table><thead><tr><th>组（因子低→高）</th><th>最终净值</th><th>累计收益</th><th>端点最大回撤</th></tr></thead>'
        f"<tbody>{table_rows}</tbody></table></div></section>"
        f"<section><h2>首期分组示例</h2><p>入场日：{first or '无'}。展示前20名成员；分组随每期因子变化，完整历史见成员明细CSV。</p>"
        f'<div class="table"><table><thead><tr><th>组</th><th>证券代码</th><th>财报报告期</th><th>{_esc(name)}</th><th>组内权重</th></tr></thead><tbody>{member_rows}</tbody></table></div></section>'
        "<section><details><summary>运行参数与覆盖情况</summary>"
        f"<pre>{details}</pre></details><h3>数据来源与研究边界</h3><ul>"
        + "".join(f"<li>{_esc(notice)}</li>" for notice in notices)
        + '</ul><p>完整数据：<a href="report.json">JSON报告</a> · <a href="memberships.csv">成员明细</a> · <a href="period_returns.csv">分组收益</a> · <a href="nav.csv">分组净值</a> · <a href="exclusions.csv">排除原因</a>。复算输入位于signals.json及market目录。</p></section></body></html>'
    )
