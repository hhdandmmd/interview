"""读取固定特征与小体量行情，输出可独立复算的输入、报告和摘要。"""

import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path

from finpipe.backtest.calculation import CODE_PATTERN, run_groups, validate_signals
from finpipe.backtest.market import write_csv
from finpipe.backtest.report import chart_data, nav_chart, render_report
from finpipe.exports import sha256, write_json
from finpipe.storage import Store
from finpipe.timeutil import iso_time, utc_now

TIME_POLICIES = ("observed", "publication_proxy")
LIMITATIONS = [
    "分组等权的收盘价格研究，不模拟停牌、涨跌停、整手、成交量或撮合；收益不代表可实现策略。",
    "固定股票池可能存在生存偏差；未验证历史上市、退市及行业成员变化。",
    "净值按非重叠持有窗口复利，回撤仅在窗口端点观察；G5-G1为收益差，非可交易多空组合。",
    "并列特征按证券代码稳定分组；并列值多时分组经济含义较弱。费用为统一单边基点数。",
    "不足分组数的决策窗口持有现金，不计现金利息，跳过次数单独披露；尾部不足H日的窗口不建仓。",
]


def project_feature(feature: dict, published_at: str, policy: str) -> dict:
    """近似可用时间取所有依赖的披露/已知更新上界，真实时间始终另列保留。"""
    actual = max(iso_time(feature["data_available_at"]), iso_time(published_at))
    source_times = [item.get("source_available_at") for item in feature["inputs"]]
    proxy_known = bool(source_times) and all(source_times)
    available = (
        max(map(iso_time, source_times))
        if proxy_known and policy == "publication_proxy"
        else actual
    )
    return {
        **{
            name: feature[name]
            for name in (
                "security_code",
                "report_end",
                "feature_name",
                "feature_value",
                "feature_status",
                "feature_version_id",
            )
        },
        "available_at": available,
        "actual_data_available_at": iso_time(feature["data_available_at"]),
        "actual_published_at": iso_time(published_at),
        "timing_status": "publication_proxy"
        if policy == "publication_proxy" and proxy_known
        else "observed",
        "reason_codes": feature.get("reason_codes", []),
        "evidence_flags": feature.get("evidence_flags", []),
    }


def load_workspace_signals(
    workspace: Path, snapshot_id: str | None, feature: str, policy: str
) -> tuple[list[dict], dict]:
    """固定同一范围和公式版本；真实时间模式读取截至指定发布的全部已发布版本。"""
    if policy not in TIME_POLICIES:
        raise ValueError("未知回测时间口径")
    with Store(workspace, readonly=True) as store:
        snapshot = store.select_snapshot(snapshot_id=snapshot_id)
        if policy == "publication_proxy":
            snapshots = [snapshot]
        else:
            snapshots = store.conn.execute(
                "SELECT * FROM dataset_snapshots WHERE scope_id=? AND profile_id=? AND status='PUBLISHED' "
                "AND publication_sequence<=? ORDER BY publication_sequence",
                (snapshot["scope_id"], snapshot["profile_id"], snapshot["publication_sequence"]),
            ).fetchall()
        rows, seen = [], set()
        for release in snapshots:
            results = store.conn.execute(
                "SELECT f.payload_json FROM snapshot_features m JOIN feature_versions f USING(feature_version_id) "
                "WHERE m.snapshot_id=? AND m.feature_name=? ORDER BY m.security_code,m.report_end",
                (release["snapshot_id"], feature),
            )
            for item in results:
                payload = json.loads(item[0])
                if payload["feature_version_id"] not in seen:
                    rows.append(project_feature(payload, release["published_at"], policy))
                    seen.add(payload["feature_version_id"])
        metadata = {
            "kind": "financial_workspace",
            "snapshot_id": snapshot["snapshot_id"],
            "scope_id": snapshot["scope_id"],
            "profile_id": snapshot["profile_id"],
            "time_policy": policy,
            "historical_versions_verified": False,
            "timing_counts": dict(Counter(row["timing_status"] for row in rows)),
            "notice": "实际观察和发布时间保留；历史原始公告版本未核验。"
            + (
                "当前版本按披露及已知更新日期近似放出，仍可能包含未知历史修订造成的前视偏差。"
                if policy == "publication_proxy"
                else "仅使用本系统实际可用的版本，不把今天抓取的财报回填至历史。"
            ),
        }
    return rows, metadata


def load_signals(path: Path, policy: str) -> tuple[list[dict], dict]:
    """外部信号须声明来源和时间口径；实际时间模式禁止提前于已知真实可用时间。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"信号文件不是合法JSON：第{exc.lineno}行第{exc.colno}列") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        raise ValueError("信号文件及metadata须为JSON对象")
    metadata = payload["metadata"]
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or metadata.get("time_policy") != policy
        or not isinstance(metadata.get("kind"), str)
        or not metadata["kind"].strip()
    ):
        raise ValueError("信号文件须声明schema_version=1、kind及匹配的time_policy")
    rows = validate_signals(payload.get("rows"))
    if policy == "observed":
        for row in rows:
            actual = [row.get(name) for name in ("actual_data_available_at", "actual_published_at")]
            if any(value and iso_time(value) > row["available_at"] for value in actual):
                raise ValueError("真实可用时间模式不能早于已知实际观察/发布时间")
    return rows, {**metadata, "input_sha256": sha256(path)}


def load_market(directory: Path) -> tuple[list[dict], list[str], dict]:
    """仅消费完整、摘要匹配且复权口径明确的行情文件，失败采集不能当作空值数据。"""
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"行情清单不是合法JSON：第{exc.lineno}行第{exc.colno}列") from exc
    if not isinstance(manifest, dict):
        raise ValueError("行情清单须为JSON对象")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("complete") is not True
    ):
        raise ValueError("行情清单未完成或版本不支持")
    if (
        manifest.get("adjustment") not in ("hfq", "synthetic")
        or not isinstance(manifest.get("calendar_kind"), str)
        or not manifest["calendar_kind"].strip()
    ):
        raise ValueError("行情须声明hfq后复权或synthetic人工口径，以及日历来源")
    codes = manifest.get("codes", [])
    if (
        not isinstance(codes, list)
        or not codes
        or any(not isinstance(code, str) or not CODE_PATTERN.fullmatch(code) for code in codes)
        or len(codes) != len(set(codes))
    ):
        raise ValueError("行情须显式声明不重复的研究股票池codes")
    if not isinstance(manifest.get("files"), dict):
        raise ValueError("行情清单files须为文件名与SHA256的对象")
    for field in ("start", "end"):
        value = manifest.get(field)
        if value is not None and (
            not isinstance(value, str) or date.fromisoformat(value).isoformat() != value
        ):
            raise ValueError(f"行情清单{field}须为ISO日期字符串")
    if manifest.get("start") and manifest.get("end") and manifest["start"] > manifest["end"]:
        raise ValueError("行情清单起始日期不能晚于结束日期")
    for name in ("prices.csv", "calendar.csv"):
        if manifest["files"].get(name) != sha256(directory / name):
            raise ValueError(f"行情文件摘要不匹配：{name}")
    with (directory / "prices.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if not {"security_code", "trade_date", "close"}.issubset(fields) or len(fields) != len(
            set(fields)
        ):
            raise ValueError("prices.csv须包含不重复的security_code、trade_date、close列")
        prices = []
        for position, row in enumerate(reader, 2):
            if None in row or any(
                row.get(field) in (None, "") for field in ("security_code", "trade_date", "close")
            ):
                raise ValueError(
                    f"prices.csv第{position}行列结构错误或必需值为空；缺报价请省略该行"
                )
            try:
                close = float(row["close"])
            except ValueError as exc:
                raise ValueError(f"prices.csv第{position}行close须为数值") from exc
            prices.append(
                {
                    "security_code": row["security_code"],
                    "trade_date": row["trade_date"],
                    "close": close,
                }
            )
    with (directory / "calendar.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if "trade_date" not in fields or len(fields) != len(set(fields)):
            raise ValueError("calendar.csv须包含不重复的trade_date列")
        calendar = []
        for position, row in enumerate(reader, 2):
            if None in row or not row.get("trade_date"):
                raise ValueError(f"calendar.csv第{position}行列结构错误或交易日为空")
            calendar.append(row["trade_date"])
    if any(row["security_code"] not in codes for row in prices):
        raise ValueError("行情包含研究股票池以外证券")
    return prices, calendar, {**manifest, "manifest_sha256": sha256(manifest_path)}


def write_report(
    output: Path, report: dict, signals: list[dict], prices: list[dict], calendar: list[str]
) -> dict:
    """输出独立复算输入与完整成员；新目录避免覆盖既有审核结果。"""
    output = output.resolve()
    if output.exists():
        raise ValueError("回测输出目录已存在，请使用新的目录，保留已有报告")
    output.mkdir(parents=True, exist_ok=False)
    write_json(
        output / "signals.json",
        {"schema_version": 1, "metadata": report["signal_metadata"], "rows": signals},
    )
    market = output / "market"
    market.mkdir()
    write_csv(market / "prices.csv", prices, ["security_code", "trade_date", "close"])
    write_csv(market / "calendar.csv", [{"trade_date": day} for day in calendar], ["trade_date"])
    metadata = {
        key: value
        for key, value in report["market_metadata"].items()
        if key not in ("requests", "files")
    }
    write_json(
        market / "manifest.json",
        {
            **metadata,
            "schema_version": 1,
            "complete": True,
            "files": {name: sha256(market / name) for name in ("prices.csv", "calendar.csv")},
        },
    )
    for name, rows, fallback in (
        ("memberships", report["memberships"], ["decision_date", "security_code", "group"]),
        ("period_returns", report["period_returns"], ["decision_date", "group", "net_return"]),
        ("nav", report["nav"], ["exit_date", "group", "nav"]),
        ("exclusions", report["exclusions"], ["decision_date", "security_code", "reason"]),
    ):
        fields = list(dict.fromkeys(key for row in rows for key in row)) or fallback
        write_csv(output / f"{name}.csv", rows, fields)
    write_json(output / "report.json", report)
    chart = nav_chart(report)
    (output / "nav.svg").write_text(chart, encoding="utf-8")
    write_csv(output / "chart_data.csv", chart_data(report), ["trade_date", "series", "nav"])
    (output / "report.html").write_text(render_report(report, chart), encoding="utf-8")
    write_json(
        output / "SHA256.json",
        {
            path.relative_to(output).as_posix(): sha256(path)
            for path in sorted(output.rglob("*"))
            if path.is_file()
        },
    )
    return {
        "status": report["status"],
        "output": str(output),
        "summary": report["summary"],
        "time_policy": report["signal_metadata"]["time_policy"],
        "report": str(output / "report.html"),
    }


def execute_backtest(arguments, workspace: Path) -> dict:
    """命令编排不修改财务库；人工样例和真实数据入口明确分开。"""
    if arguments.demo:
        if (
            arguments.signals
            or arguments.market
            or arguments.snapshot
            or arguments.time_policy != "observed"
        ):
            raise ValueError("人工demo不能混入真实信号、行情、快照或公告近似模式")
        from finpipe.backtest.sample import make_sample

        signals, prices, calendar, signal_metadata = make_sample()
        market_metadata = {
            "kind": "synthetic",
            "adjustment": "synthetic",
            "calendar_kind": "synthetic",
            "codes": sorted({row["security_code"] for row in signals}),
            "notice": "人工行情和日期序列，仅演示计算正确性。",
        }
    else:
        if not arguments.market:
            raise ValueError("真实/外部回测须指定 --market 行情目录；可先用market命令小范围获取")
        if arguments.signals and arguments.snapshot:
            raise ValueError("外部信号与库内快照不能同时指定")
        signals, signal_metadata = (
            load_signals(arguments.signals, arguments.time_policy)
            if arguments.signals
            else load_workspace_signals(
                workspace, arguments.snapshot, arguments.feature, arguments.time_policy
            )
        )
        prices, calendar, market_metadata = load_market(arguments.market)
        left = market_metadata.get("start", calendar[0] if calendar else None)
        right = market_metadata.get("end", calendar[-1] if calendar else None)
        if (arguments.start and left and arguments.start < left) or (
            arguments.end and right and arguments.end > right
        ):
            raise ValueError("请求范围超出行情清单覆盖范围，请补充行情后回测")
        signal_metadata = {
            **signal_metadata,
            "original_security_count": len({row["security_code"] for row in signals}),
            "selected_codes": market_metadata["codes"],
            "pool_selection": "显式使用行情清单codes作为固定研究池，不按未来报价完整性筛选",
        }
        signals = [row for row in signals if row["security_code"] in market_metadata["codes"]]
    report = run_groups(
        signals,
        prices,
        calendar,
        feature=arguments.feature,
        start=arguments.start,
        end=arguments.end,
        horizon=arguments.horizon
        if arguments.horizon is not None
        else (2 if arguments.demo else 20),
        groups=arguments.groups,
        cost_bps=arguments.cost_bps,
        codes=market_metadata["codes"],
    )
    report.update(
        schema_version=1,
        generated_at=utc_now(),
        signal_metadata=signal_metadata,
        market_metadata=market_metadata,
        limitations=LIMITATIONS,
    )
    return write_report(arguments.output, report, signals, prices, calendar)
