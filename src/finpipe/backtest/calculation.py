"""纯计算分组回测；未知收益保留NULL，日历、信号和价格均由调用方提供。"""

import math
import re
from collections import Counter
from datetime import date

import numpy as np
import pandas as pd

from finpipe.timeutil import SHANGHAI, iso_time, parse_time, quarter_end

CODE_PATTERN = re.compile(r"\d{6}\.(SH|SZ|BJ)\Z")


def validate_signals(rows: list[dict]) -> list[dict]:
    """校验信号结构、报告期与可用时间；失效版本保留，防止回退到旧好值。"""
    if not isinstance(rows, list):
        raise ValueError("信号rows须为列表，每行须为对象")
    text_fields = (
        "security_code",
        "report_end",
        "feature_name",
        "feature_status",
        "feature_version_id",
        "available_at",
    )
    result, identities = [], set()
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"信号第{position}行须为对象")
        if "feature_value" not in row:
            raise ValueError(f"信号第{position}行缺少feature_value；缺值须显式填写NULL")
        for field in text_fields:
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"信号第{position}行{field}须为非空字符串")
        item = dict(row)
        code = item["security_code"]
        if not CODE_PATTERN.fullmatch(code):
            raise ValueError("证券代码须保留交易所后缀，如600519.SH")
        item["report_end"] = quarter_end(item["report_end"])
        item["available_at"] = iso_time(item["available_at"])
        available_day = parse_time(item["available_at"]).astimezone(SHANGHAI).date()
        if date.fromisoformat(item["report_end"]) > available_day:
            raise ValueError("财报报告期不能晚于信号可用时间的上海日期")
        for field in ("actual_data_available_at", "actual_published_at"):
            actual = item.get(field)
            if actual is not None:
                if not isinstance(actual, str) or not actual:
                    raise ValueError(f"信号第{position}行{field}须为时间字符串或NULL")
                item[field] = iso_time(actual)
        value = item["feature_value"]
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("特征值须为有限数或NULL")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError("特征值须为有限数或NULL")
        identity = (code, item["report_end"], item["feature_name"], item["available_at"])
        if identity in identities:
            raise ValueError("同一特征在同一可用时间出现多个版本，须先解决冲突")
        identities.add(identity)
        result.append(item)
    return result


def _rank_ic(values: list[float], returns: list[float | None]) -> float | None:
    """并列值用平均秩；缺少任一成员收益或截面常数时不报告IC。"""
    if len(values) < 3 or any(value is None for value in returns):
        return None
    left, right = pd.Series(values).rank(), pd.Series(returns).rank()
    if left.nunique() < 2 or right.nunique() < 2:
        return None
    return float(left.corr(right))


def run_groups(
    signals: list[dict],
    prices: list[dict],
    calendar: list[str],
    *,
    feature: str,
    start: str | None = None,
    end: str | None = None,
    horizon: int = 20,
    groups: int = 5,
    cost_bps: float = 0.0,
    codes: list[str] | None = None,
) -> dict:
    """T日15点选股，下一交易日收盘进入，持有H个交易日；窗口不重叠。

    每期等权、期末全部退出；双边费用各按成交金额扣除。缺失端点价格使整组
    当期收益未知，累计净值从该期停止，不能删除缺失成员或跳过未知损益。
    """
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError("持有交易日数须为正整数")
    if isinstance(groups, bool) or not isinstance(groups, int) or not 2 <= groups <= 20:
        raise ValueError("分组数须在2到20之间")
    if (
        isinstance(cost_bps, bool)
        or not isinstance(cost_bps, (int, float))
        or not 0 <= cost_bps < 10000
        or not math.isfinite(cost_bps)
    ):
        raise ValueError("单边费用须为0至10000之间的有限基点数")
    if (
        not isinstance(calendar, list)
        or not calendar
        or any(not isinstance(day, str) for day in calendar)
        or calendar != sorted(set(calendar))
    ):
        raise ValueError("交易日历须非空、升序且无重复；不能用个股有报价日期替代")
    for day in calendar:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("交易日期须为ISO日期")
    start, end = start or calendar[0], end or calendar[-1]
    for boundary in (start, end):
        if not isinstance(boundary, str) or date.fromisoformat(boundary).isoformat() != boundary:
            raise ValueError("回测起止范围须为ISO日期字符串")
    if start > end:
        raise ValueError("回测起始日期不能晚于结束日期")
    selected_days = [i for i, day in enumerate(calendar) if start <= day <= end]
    if not selected_days:
        raise ValueError("回测范围没有交易日")
    if not isinstance(prices, list):
        raise ValueError("行情须为记录列表")
    price_map = {}
    for row in prices:
        if not isinstance(row, dict) or not {"security_code", "trade_date", "close"}.issubset(row):
            raise ValueError("每条行情须为包含security_code、trade_date、close的对象")
        code, day, close = row["security_code"], row["trade_date"], row["close"]
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code) or day not in calendar:
            raise ValueError("行情证券代码或交易日期不符合输入契约")
        if isinstance(close, bool) or not isinstance(close, (int, float)):
            raise ValueError("收盘价须为有限正数；缺失报价请省略该行")
        try:
            finite = math.isfinite(close)
        except OverflowError:
            finite = False
        if not finite or close <= 0 or (code, day) in price_map:
            raise ValueError("收盘价须为有限正数且证券日期键不能重复")
        price_map[code, day] = close
    signal_rows = [row for row in validate_signals(signals) if row["feature_name"] == feature]
    if not signal_rows:
        raise ValueError("没有指定特征的信号")
    if codes is not None and (
        not isinstance(codes, list) or any(not isinstance(code, str) for code in codes)
    ):
        raise ValueError("研究股票池须为完整证券代码的列表")
    universe = sorted(codes if codes is not None else {row["security_code"] for row in signal_rows})
    if len(universe) != len(set(universe)) or any(
        not CODE_PATTERN.fullmatch(code) for code in universe
    ):
        raise ValueError("研究股票池须为不重复的完整证券代码")
    memberships, periods, excluded, nav_rows = [], [], [], []
    nav = {group: 1.0 for group in range(1, groups + 1)}
    peak, drawdown = dict(nav), dict.fromkeys(nav, 0.0)
    skipped, complete_periods = Counter(), 0
    incomplete = False
    fee = cost_bps / 10000
    for index in range(selected_days[0], selected_days[-1] + 1, horizon):
        decision = calendar[index]
        if index + 1 + horizon > selected_days[-1]:
            skipped["insufficient_forward_calendar"] += 1
            continue
        cutoff = iso_time(decision + "T15:00:00+08:00")
        latest = {}
        for row in signal_rows:
            if row["available_at"] <= cutoff:
                code = row["security_code"]
                order = (row["report_end"], row["available_at"])
                if code not in latest or order > (
                    latest[code]["report_end"],
                    latest[code]["available_at"],
                ):
                    latest[code] = row
        usable = []
        for code in universe:
            row = latest.get(code)
            reason = "not_yet_available" if row is None else row["feature_status"]
            if row and row["feature_value"] is None and reason in ("ok", "warning"):
                reason = "null_value"
            if row and row["feature_value"] is not None and reason in ("ok", "warning"):
                usable.append(row)
            else:
                excluded.append(
                    {"decision_date": decision, "security_code": code, "reason": reason}
                )
        if len(usable) < groups:
            skipped["insufficient_factor_members"] += 1
            continue
        usable.sort(key=lambda row: (row["feature_value"], row["security_code"]))
        entry, exit_day = calendar[index + 1], calendar[index + 1 + horizon]
        forward, group_returns = [], []
        for group, indices in enumerate(np.array_split(np.arange(len(usable)), groups), 1):
            member_returns = []
            for position in indices:
                signal = usable[int(position)]
                code = signal["security_code"]
                entry_price, exit_price = (
                    price_map.get((code, entry)),
                    price_map.get((code, exit_day)),
                )
                gross = exit_price / entry_price - 1 if entry_price and exit_price else None
                if gross is not None and not math.isfinite(gross):
                    raise ValueError("端点价格比产生非有限收益，请核查价格单位与极端值")
                member_returns.append(gross)
                forward.append(gross)
                memberships.append(
                    {
                        **signal,
                        "decision_date": decision,
                        "entry_date": entry,
                        "exit_date": exit_day,
                        "group": group,
                        "weight": 1 / len(indices),
                        "entry_close": entry_price,
                        "exit_close": exit_price,
                        "gross_return": gross,
                        "price_status": "ok" if gross is not None else "missing_endpoint",
                    }
                )
            gross = (
                sum(member_returns) / len(member_returns)
                if all(value is not None for value in member_returns)
                else None
            )
            net = (1 - fee) ** 2 * (1 + gross) - 1 if gross is not None else None
            if net is None:
                incomplete = True
                nav[group] = None
            elif nav[group] is not None:
                nav[group] *= 1 + net
                if not math.isfinite(nav[group]):
                    raise ValueError("累计净值溢出，请核查价格单位与研究范围")
                peak[group] = max(peak[group], nav[group])
                drawdown[group] = min(drawdown[group], nav[group] / peak[group] - 1)
            group_returns.append(net)
            periods.append(
                {
                    "decision_date": decision,
                    "entry_date": entry,
                    "exit_date": exit_day,
                    "group": group,
                    "members": len(indices),
                    "missing_price_members": sum(value is None for value in member_returns),
                    "gross_return": gross,
                    "net_return": net,
                }
            )
            nav_rows.append({"exit_date": exit_day, "group": group, "nav": nav[group]})
        complete_periods += 1
        for row in periods[-groups:]:
            row.update(
                rank_ic=_rank_ic([item["feature_value"] for item in usable], forward),
                cohort_return=sum(forward) / len(forward)
                if all(value is not None for value in forward)
                else None,
                high_minus_low=group_returns[-1] - group_returns[0]
                if group_returns[-1] is not None and group_returns[0] is not None
                else None,
                distinct_factor_values=len({item["feature_value"] for item in usable}),
                warning_members=sum(item["feature_status"] == "warning" for item in usable),
            )
    return {
        "status": "ok" if complete_periods and not incomplete else "incomplete",
        "parameters": {
            "feature": feature,
            "start": start,
            "end": end,
            "horizon": horizon,
            "groups": groups,
            "cost_bps": cost_bps,
            "decision_time": "15:00:00+08:00",
            "entry": "next_session_close",
            "weighting": "equal_weight",
        },
        "summary": {
            "formed_periods": complete_periods,
            "first_entry_date": periods[0]["entry_date"] if periods else None,
            "last_exit_date": periods[-1]["exit_date"] if periods else None,
            "calendar_first_session": calendar[selected_days[0]],
            "calendar_last_session": calendar[selected_days[-1]],
            "skipped_decisions": dict(skipped),
            "exclusion_counts": dict(Counter(row["reason"] for row in excluded)),
            "universe_size": len(universe),
            "groups": [
                {
                    "group": group,
                    "cumulative_return": nav[group] - 1
                    if complete_periods and nav[group] is not None
                    else None,
                    "max_drawdown_at_period_ends": drawdown[group]
                    if complete_periods and nav[group] is not None
                    else None,
                }
                for group in nav
            ],
        },
        "memberships": memberships,
        "period_returns": periods,
        "nav": nav_rows,
        "exclusions": excluded,
    }
