"""统一命令入口：参数错误、阻断和恢复均返回结构化结果。"""

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from filelock import Timeout

from finpipe import __version__
from finpipe.config import load_config
from finpipe.pipeline import execute_manifest, execute_online, plan_manifest, resume_run
from finpipe.storage import Store
from finpipe.timeutil import iso_time, quarter_end


def parser() -> argparse.ArgumentParser:
    """各命令共享工作区和配置参数，帮助不依赖当前目录。"""
    root = argparse.ArgumentParser(prog="finpipe", description="可追溯财报处理与特征工程")
    root.add_argument("--version", action="version", version=f"finpipe {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("demo", "离线运行真实多期样例"),
        ("run", "在线采集并处理"),
        ("replay", "重放固定归档或库内证据"),
        ("resume", "恢复原冻结运行"),
        ("query", "查询固定发布快照"),
        ("explain", "解释一个特征的输入与原因"),
        ("export", "复制指定发布包"),
        ("health", "独立只读健康检查"),
        ("alerts", "查看或确认本地告警"),
        ("doctor", "诊断与有限修复"),
        ("backup", "一致备份并核验"),
        ("reconcile", "核对固定快照与独立报告参考"),
        ("market", "获取小范围后复权日线并归档证据"),
        ("backtest", "财务特征等权分组回测并输出报告"),
    ):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--workspace", type=Path)
        command.add_argument("--config", type=Path)
        if name in ("replay", "resume"):
            command.add_argument("--run-id")
        if name == "replay":
            command.add_argument("--manifest", type=Path)
            command.add_argument("--from-db", action="store_true")
            command.add_argument("--allow-synthetic", action="store_true")
            command.add_argument("--profile")
        if name == "run":
            command.add_argument("--sample-size", type=int)
            command.add_argument("--refresh", choices=("recent", "all"), default="all")
        if name in ("query", "explain", "export", "reconcile"):
            command.add_argument("--snapshot")
        if name in ("query", "explain", "replay"):
            command.add_argument("--scope")
        if name in ("query", "explain"):
            command.add_argument("--company")
            command.add_argument("--period")
            command.add_argument("--feature")
            command.add_argument("--as-of")
            command.add_argument("--allow-stale", action="store_true")
        if name in ("export", "backup", "market", "backtest"):
            command.add_argument("--output", type=Path, required=True)
        if name in ("market", "backtest"):
            command.add_argument("--start")
            command.add_argument("--end")
        if name == "market":
            command.add_argument("--codes", required=True, help="逗号分隔，如600519.SH,000333.SZ")
        if name == "backtest":
            command.add_argument("--demo", action="store_true", help="人工财报与价格的离线演示")
            command.add_argument("--signals", type=Path, help="外部signals.json；省略则读工作区")
            command.add_argument(
                "--market", type=Path, help="含manifest、prices、calendar的行情目录"
            )
            command.add_argument("--snapshot")
            command.add_argument("--feature", default="roe_weighted_ytd")
            command.add_argument(
                "--time-policy", choices=("observed", "publication_proxy"), default="observed"
            )
            command.add_argument("--horizon", type=int)
            command.add_argument("--groups", type=int, default=5)
            command.add_argument("--cost-bps", type=float, default=0.0, help="单边费用，单位基点")
        if name == "doctor":
            command.add_argument("--repair", action="store_true")
        if name == "alerts":
            command.add_argument("--ack")
        if name == "reconcile":
            command.add_argument("--reference", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
    return root


def _run(arguments, config: dict) -> dict:
    command = arguments.command
    workspace = Path(config["workspace"])
    if command == "market":
        from finpipe.backtest.market import fetch_market

        if not arguments.start or not arguments.end:
            raise ValueError("market须指定 --start 和 --end ISO日期")
        return fetch_market(
            [code.strip() for code in arguments.codes.split(",")],
            arguments.start,
            arguments.end,
            arguments.output,
            config["network"],
        )
    if command == "backtest":
        from finpipe.backtest.io import execute_backtest

        return execute_backtest(arguments, workspace)
    if command == "demo":
        source = Path(__file__).parent / "resources/demo/manifest.json"
        return execute_manifest(source, config)
    if command == "run":
        if arguments.sample_size is not None:
            universe = json.loads(
                (Path(__file__).parent / "resources/demo/universe/universe.json").read_text(
                    encoding="utf-8"
                )
            )
            if not 1 <= arguments.sample_size <= len(universe["codes"]):
                raise ValueError("样本数须在冻结名单的1至50之间")
            config["scope"]["codes"] = universe["codes"][: arguments.sample_size]
        return execute_online(config, arguments.refresh)
    if command == "resume":
        if not arguments.run_id:
            raise ValueError("resume必须指定 --run-id")
        return resume_run(workspace, arguments.run_id)
    if command == "replay":
        if sum((bool(arguments.manifest), bool(arguments.run_id), arguments.from_db)) != 1:
            raise ValueError("--manifest、--run-id、--from-db必须恰选一个")
        manifest = arguments.manifest
        frozen_scope = None
        with Store(workspace) as store:
            if arguments.scope:
                frozen_scope = json.loads(store.scope(arguments.scope)["content_json"])
            if arguments.profile:
                config["profile"] = store.profile(arguments.profile)
                config["profile_id"] = arguments.profile
            if arguments.run_id:
                row = store.conn.execute(
                    "SELECT plan_json FROM runs WHERE run_id=?", (arguments.run_id,)
                ).fetchone()
                if not row:
                    raise ValueError("运行不存在")
                manifest = plan_manifest(store, json.loads(row[0]))
                frozen_scope = json.loads(row[0]).get("scope")
            if arguments.from_db:
                scope = store.scope(arguments.scope)
                row = store.conn.execute(
                    "SELECT plan_json FROM runs WHERE scope_id=? ORDER BY started_at DESC LIMIT 1",
                    (scope["scope_id"],),
                ).fetchone()
                if not row:
                    raise ValueError("该范围没有冻结输入")
                manifest = plan_manifest(store, json.loads(row[0]))
                frozen_scope = json.loads(row[0]).get("scope")
        return execute_manifest(
            manifest, config, arguments.allow_synthetic, arguments.run_id, frozen_scope
        )
    if command in ("query", "explain"):
        with Store(workspace, readonly=True) as store:
            snapshot = store.select_snapshot(
                arguments.scope,
                arguments.snapshot,
                iso_time(arguments.as_of) if arguments.as_of else None,
                arguments.allow_stale,
            )
            features = store.features(
                snapshot["snapshot_id"],
                arguments.company,
                quarter_end(arguments.period) if arguments.period else None,
                arguments.feature,
            )
            if command == "explain" and len(features) != 1:
                raise ValueError("explain须指定唯一公司、报告期和特征")
            rows = []
            for feature in features:
                row = asdict(feature)
                if command == "query":
                    row.pop("inputs")
                rows.append(row)
            return {
                "status": "ok",
                "snapshot_id": snapshot["snapshot_id"],
                "scope_id": snapshot["scope_id"],
                "profile_id": snapshot["profile_id"],
                "published_at": snapshot["published_at"],
                "health": store.scope(snapshot["scope_id"])["health_status"],
                "rows": rows,
            }
    if command == "export":
        from finpipe.ops import validate_published

        with Store(workspace, readonly=True) as store:
            snapshot = store.select_snapshot(snapshot_id=arguments.snapshot)
            source = workspace / "snapshots" / snapshot["snapshot_id"]
            validate_published(store, snapshot["snapshot_id"])
            target = arguments.output.resolve()
            if target.exists():
                raise ValueError("导出目标已存在，不覆盖已有发布包")
            shutil.copytree(source, target)
            return {"status": "ok", "snapshot_id": snapshot["snapshot_id"], "output": str(target)}
    if command == "reconcile":
        from finpipe.catalog import FIELDS
        from finpipe.exports import frames, sha256, write_json
        from finpipe.reconciliation import compare_reference

        with Store(workspace, readonly=True) as store:
            snapshot = store.select_snapshot(snapshot_id=arguments.snapshot)
            left = []
            for table, frame in frames(store, snapshot["snapshot_id"]).items():
                if table not in FIELDS:
                    continue
                for record in frame.to_dict("records"):
                    states = json.loads(record["field_states"])
                    clean = store.conn.execute(
                        "SELECT payload_json FROM clean_versions WHERE clean_version_id=?",
                        (record["clean_version_id"],),
                    ).fetchone()
                    clean_payload = json.loads(clean[0]) if clean else {}
                    for spec in FIELDS[table]:
                        value = record[spec.name]
                        left.append(
                            {
                                "security_code": record["security_code"],
                                "report_end": record["report_end"],
                                "statement_type": table,
                                "field_name": spec.name,
                                "value": None if value is None or value != value else value,
                                "unit": spec.unit,
                                "consolidation_scope": "consolidated",
                                "field_status": states.get(spec.name, "missing"),
                                "quality_status": record["quality_status"],
                                "time_valid": clean_payload.get("time_valid", False),
                                "period_basis": spec.period_basis,
                                "clean_version_id": record["clean_version_id"],
                                "record_state_id": record["record_state_id"],
                            }
                        )
            reference = json.loads(arguments.reference.read_text(encoding="utf-8"))
            report = compare_reference(left, reference)
            report.update(
                snapshot_id=snapshot["snapshot_id"],
                profile_id=snapshot["profile_id"],
                reference_sha256=sha256(arguments.reference),
            )
            if arguments.output.exists():
                raise ValueError("核对报告目标已存在，请保留原报告使用新路径")
            write_json(arguments.output, report)
            return {
                "status": "ok"
                if not report["counts"].get("mismatch") and report["match_rate"] is not None
                else "failed",
                "output": str(arguments.output),
                **{name: value for name, value in report.items() if name != "rows"},
            }
    if command in ("health", "doctor", "backup"):
        from finpipe.ops import backup, doctor, health

        if command == "health":
            return health(workspace, config["job"] if arguments.config else None)
        if command == "doctor":
            return doctor(workspace, arguments.repair)
        return backup(workspace, arguments.output)
    if command == "alerts":
        with Store(workspace, readonly=not bool(arguments.ack)) as store:
            if arguments.ack:
                from finpipe.pipeline import workspace_lock

                with workspace_lock(workspace), store.transaction():
                    if not store.conn.execute(
                        "UPDATE alerts SET status='acknowledged' WHERE alert_id=? AND status='open'",
                        (arguments.ack,),
                    ).rowcount:
                        raise ValueError("没有该未确认告警")
            return {
                "status": "ok",
                "rows": [
                    dict(r)
                    for r in store.conn.execute("SELECT * FROM alerts ORDER BY opened_at DESC")
                ],
            }
    raise ValueError("未知命令")


def main(argv: list[str] | None = None) -> int:
    """程序正常结束返回退出码；不用input阻塞调度或自动化。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    arguments = parser().parse_args(argv)
    try:
        config = load_config(arguments.config, arguments.workspace)
        result = _run(arguments, config)
        if result.get("run_id") and result.get("status") in ("FAILED", "BLOCKED", "WAITING_DATA"):
            prefix = f'& "{sys.executable}"' + (
                "" if getattr(sys, "frozen", False) else " -m finpipe"
            )
            result["recovery_command_powershell"] = (
                f'{prefix} resume --run-id "{result["run_id"]}" --workspace "{config["workspace"]}"'
            )
        code = 0 if result.get("status") in ("ok", "healthy", "PUBLISHED", "NO_CHANGE") else 3
        if result.get("scope_health") in ("blocked", "stale"):
            code = 3
    except Timeout:
        result, code = {"status": "FAILED", "error": "工作区正被另一进程写入，OS锁未释放"}, 4
    except (ValueError, OSError, KeyError, RuntimeError, sqlite3.Error) as exc:
        result, code = {"status": "FAILED", "error": str(exc)}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code
