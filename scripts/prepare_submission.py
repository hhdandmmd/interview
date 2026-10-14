"""离线生成题目回应的示例结果；真实归档、公开回测与人工反例分别标识。"""

import argparse
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from finpipe.backtest.io import load_market, load_signals, run_backtest
from finpipe.cli import main as cli_main
from finpipe.config import load_config
from finpipe.exports import sha256, validate_files, write_json
from finpipe.identity import stable_id
from finpipe.models import BusinessKey, SourceRecord
from finpipe.ops import doctor
from finpipe.pipeline import execute_manifest
from finpipe.storage import Store
from finpipe.timeutil import utc_now
from finpipe.transforms import normalize

ROOT = Path(__file__).resolve().parents[1]


def invoke(arguments: list[str]) -> dict:
    """复用正式CLI，保存实际返回结果；失败时停止，避免生成成功样例。"""
    with redirect_stdout(io.StringIO()) as stream:
        code = cli_main(arguments)
    result = json.loads(stream.getvalue())
    if code:
        raise RuntimeError(result)
    return result


def cleaning_cases(profile: dict) -> list[dict]:
    """人工输入和预期状态直接声明，不混入真实财务库或真实统计。"""
    income = dict(
        OPERATE_INCOME=100,
        TOTAL_OPERATE_INCOME=100,
        OPERATE_COST=60,
        NETPROFIT=10,
        PARENT_NETPROFIT=8,
    )
    balance = dict(
        TOTAL_ASSETS=100,
        TOTAL_LIABILITIES=30,
        TOTAL_EQUITY=70,
        TOTAL_PARENT_EQUITY=60,
        TOTAL_CURRENT_ASSETS=50,
        TOTAL_CURRENT_LIAB=20,
    )
    indicator = dict(ROEJQ=10, XSMLL=40, XSJLL=10, ZCFZL=30, LD=2.5)
    cases = (
        (
            "未披露利润保留NULL",
            "income",
            income,
            {"NETPROFIT": "未披露"},
            "net_profit_ytd",
            "missing",
            None,
        ),
        (
            "坏数字不会填零",
            "income",
            income,
            {"OPERATE_INCOME": "坏数字"},
            "operating_revenue_ytd",
            "invalid",
            None,
        ),
        (
            "负资产被标记无效",
            "balance",
            balance,
            {"TOTAL_ASSETS": -100},
            "total_assets",
            "invalid",
            None,
        ),
        (
            "极端ROE保留值并标记警告",
            "indicator",
            indicator,
            {"ROEJQ": 150},
            "roe_weighted_ytd",
            "warning",
            1.5,
        ),
        (
            "会计恒等式不匹配",
            "balance",
            balance,
            {"TOTAL_EQUITY": 50},
            "total_assets",
            "warning",
            100,
        ),
    )
    results = []
    for title, table, base, changes, field, expected_state, expected_value in cases:
        raw = {
            **base,
            **changes,
            "CURRENCY": "CNY",
            "NOTICE_DATE": "2025-04-20",
            "UPDATE_DATE": "2025-04-20",
            "_evidence": {
                "consolidation_scope": "consolidated",
                "data_kind": "synthetic",
                "说明": "人工构造，未发起HTTP请求",
            },
        }
        key = BusinessKey("eastmoney", "000999.SZ", "2024-12-31", table)
        source = SourceRecord(stable_id("synthetic_source", raw), key, raw, "2025-05-01T08:00:00Z")
        clean = normalize(source, profile, utc_now())
        assert clean.field_states[field] == expected_state and clean.values[field] == expected_value
        results.append(
            {
                "案例": title,
                "data_kind": "synthetic",
                "时间说明": "观察时间为人工夹具时间",
                "关注字段": field,
                "人工预期": {"state": expected_state, "value": expected_value},
                "输入": asdict(source),
                "实际输出": asdict(clean),
            }
        )
    return results


def prepare(destination: Path) -> dict:
    """在新目录生成可展示结果；原归档及之前的交付证据不覆盖。"""
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("示例目录已存在，请用--output指定新目录；已有结果不覆盖")
    destination.mkdir(parents=True)
    manifest = ROOT / "src/finpipe/resources/demo/manifest.json"
    original = json.loads(manifest.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="finpipe_submission_") as temporary:
        workspace = Path(temporary) / "financial"
        config = load_config(ROOT / "config/demo.toml", workspace)
        first = execute_manifest(manifest, config)
        if first["status"] != "PUBLISHED":
            raise RuntimeError(first)
        financial = destination / "财务处理"
        shutil.copytree(Path(first["export_path"]), financial)
        validate_files(financial, first["snapshot_id"])
        with Store(workspace, readonly=True) as store:
            rows = []
            for row in store.conn.execute(
                "SELECT payload_json FROM clean_versions ORDER BY clean_version_id"
            ):
                payload = json.loads(row[0])
                rows.append(
                    {
                        **payload["key"],
                        "clean_version_id": payload["clean_version_id"],
                        "source_version_id": payload["source_version_id"],
                        "quality_status": payload["quality_status"],
                        "processed_at": payload["processed_at"],
                        "field_states": json.dumps(payload["field_states"], ensure_ascii=False),
                        "reason_codes": json.dumps(payload["reasons"], ensure_ascii=False),
                    }
                )
        pd.DataFrame(rows).to_csv(financial / "清洗日志.csv", index=False, encoding="utf-8-sig")
        write_json(
            financial / "单公司查询.json",
            invoke(
                [
                    "query",
                    "--workspace",
                    str(workspace),
                    "--company",
                    "600519.SH",
                    "--period",
                    "2025Q4",
                ]
            ),
        )
        write_json(
            financial / "因子解释.json",
            invoke(
                [
                    "explain",
                    "--workspace",
                    str(workspace),
                    "--company",
                    "600519.SH",
                    "--period",
                    "2025Q4",
                    "--feature",
                    "revenue_growth_yoy",
                ]
            ),
        )
        independent = invoke(
            [
                "reconcile",
                "--workspace",
                str(workspace),
                "--reference",
                str(ROOT / "data/reference/original_reports/reference_contract.json"),
                "--output",
                str(destination / "原报告核对.json"),
            ]
        )
        repeat = execute_manifest(manifest, config)
        if repeat["status"] != "NO_CHANGE" or repeat["snapshot_id"] != first["snapshot_id"]:
            raise RuntimeError("重复重放未满足增量复用契约")
        integrity = doctor(workspace)
        if integrity["status"] != "ok":
            raise RuntimeError(integrity)
        write_json(
            destination / "清洗反例.json",
            {"data_kind": "synthetic", "cases": cleaning_cases(config["profile"])},
        )
    inputs = ROOT / "data/examples/backtest_real"
    signals, metadata = load_signals(inputs / "signals.json", "publication_proxy")
    backtest = run_backtest(
        signals,
        metadata,
        load_market(inputs / "market"),
        feature="roe_weighted_ytd",
        output=destination / "分组回测",
        start="2023-01-01",
        end="2025-12-31",
        horizon=20,
    )
    summary = {
        "generated_at": utc_now(),
        "financial_data_kind": "offline_replay_real",
        "source_manifest": "src/finpipe/resources/demo/manifest.json",
        "source_manifest_sha256": sha256(manifest),
        "original_captured_at": original["captured_at"],
        "companies": original["codes"],
        "input_quarters": len(original["periods"]),
        "首次重放": first,
        "重复重放": repeat,
        "数据库完整性": integrity,
        "独立原报告核对": independent,
        "公开行情回测": backtest,
        "限制": [
            "历史财报版本未核验，公告近似不等于严格PIT",
            "不单独核验现金分红与可成交性",
            "人工反例不是上游真实数据",
            "完整Windows干净VM尚未验收",
        ],
    }
    write_json(destination / "运行验收.json", summary)
    (destination / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>题目示例结果</title>'
        "<style>body{max-width:900px;margin:40px auto;padding:0 20px;font:16px/1.8 sans-serif;color:#16324a}"
        "a{color:#0369a1}li{margin:12px 0}</style><h1>金融数据处理与特征工程 · 示例结果</h1>"
        "<p>真实财报：茅台、美的、格力，2024—2025年输入；输出2025年15个因子。来源于固定HTTP归档，保留原观察时间。</p>"
        '<ul><li><a href="财务处理/report.html">真实财务处理报告</a> · '
        '<a href="财务处理/features_long.csv">完整特征表</a> · <a href="财务处理/dictionary.md">字段与公式字典</a></li>'
        '<li><a href="财务处理/清洗日志.csv">逐条清洗日志</a> · <a href="财务处理/quality.json">真实质量报告</a></li>'
        '<li><a href="原报告核对.json">独立原报告核对结果</a> · <a href="运行验收.json">首次与重复运行验收</a></li>'
        '<li><a href="分组回测/report.html">公开行情五组回测</a> · <a href="分组回测/nav.svg">独立净值图</a></li>'
        '<li><a href="清洗反例.json">人工清洗反例：NULL、非法值与异常标记</a></li></ul>'
        "<p>回测因子为披露累计加权ROE。公告近似研究的历史版本未核验；不得称为严格PIT或可成交策略收益。</p>"
        "<p>在浏览器中打开本页即可查看，无需网络或Python。逐项题目回应见上一级README.md。</p></html>",
        encoding="utf-8",
    )
    write_json(
        destination / "文件清单.json",
        {
            "files": [
                {
                    "path": path.relative_to(destination).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
                for path in sorted(destination.rglob("*"))
                if path.is_file()
            ]
        },
    )
    return {"status": "ok", "output": str(destination), "index": str(destination / "index.html")}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "题目回应/示例结果")
    print(json.dumps(prepare(parser.parse_args().output), ensure_ascii=False, indent=2))
