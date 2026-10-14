"""简单入口复用完整财务流水线；按配置追加独立因子回测。"""

import runpy
import sqlite3
import sys
from pathlib import Path

from filelock import Timeout

from finpipe.config import BacktestConfig, RunConfig
from finpipe.features import FEATURE_NAMES
from finpipe.pipeline import execute_online
from finpipe.runtime import recovery_command, result_exit_code


def _execute(config: RunConfig, prepared: dict) -> dict:
    """先冻结回测参数；仅在财报成功后使用该次快照，回测失败不撤销财报结果。"""
    options = config.backtest.parameters(prepared["scope"]["codes"], prepared["output_periods"])
    result = execute_online(prepared)
    if options is not None and result_exit_code(result) == 0:
        from finpipe.backtest.batch import run_selected

        result["backtest"] = run_selected(
            Path(prepared["workspace"]),
            result["snapshot_id"],
            prepared["scope"]["codes"],
            options,
            prepared["network"],
        )
    return result


def run(config: RunConfig | None = None) -> dict:
    """同步完成财报与全部因子；按配置追加独立多因子回测，返回各阶段结果。"""
    config = RunConfig() if config is None else config
    if not isinstance(config, RunConfig):
        raise ValueError("运行配置须为RunConfig")
    return _execute(config, config.to_pipeline_config())


def load_settings(path: Path) -> RunConfig:
    """读取用户维护的本地Python配置；只接受明确的CONFIG，缺失时不退回默认公司池。"""
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"未找到配置文件：{path}；请保留项目根目录config.py，或用--settings指定")
    try:
        namespace = runpy.run_path(str(path))
    except SyntaxError as exc:
        raise ValueError(f"配置文件第{exc.lineno}行语法错误：{exc.msg}") from exc
    except (TypeError, NameError, ImportError) as exc:
        raise ValueError(f"配置文件无法加载：{exc}") from exc
    config = namespace.get("CONFIG")
    if not isinstance(config, RunConfig):
        raise ValueError("config.py须定义CONFIG = RunConfig(...)")
    # 文件入口不随启动目录变化，和程序直接调用的相对路径语义区分。
    if isinstance(config.output_dir, (str, Path)) and str(config.output_dir).strip():
        config.output_dir = path.parent / config.output_dir
    if isinstance(config.backtest, BacktestConfig):
        market = config.backtest.market_dir
        if isinstance(market, (str, Path)) and str(market).strip():
            config.backtest.market_dir = path.parent / market
    return config


def main(settings: RunConfig | Path) -> int:
    """以中文显示配置、完成状态与产物路径；配置错误、采集失败和写锁均保留退出码。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        config = load_settings(settings) if isinstance(settings, Path) else settings
        if not isinstance(config, RunConfig):
            raise ValueError("运行配置须为RunConfig")
        prepared = config.to_pipeline_config()
        print(f"数据来源：东方财富；股票：{', '.join(config.stocks)}", flush=True)
        print(f"输出报告期：{prepared['output_periods'][0]} 至 {prepared['output_periods'][-1]}")
        print(f"自动采集历史：{prepared['input_periods'][0]} 至 {prepared['input_periods'][-1]}")
        print(
            f"正在运行：采集财报 → 清洗与存储 → 计算{len(FEATURE_NAMES)}个因子 → 质量检查与报告。",
            flush=True,
        )
        if config.backtest.enabled:
            print(f"随后回测：{', '.join(config.backtest.factors)}；每个因子独立分组。", flush=True)
            notice = (
                "按本系统真实已知时间放出，本次抓取不会回填过去。"
                if config.backtest.time_policy == "observed"
                else "按全部依赖的公告及已知更新日期近似放出，历史修订版本未核验。"
            )
            print(f"回测时间口径：{config.backtest.time_policy}；{notice}")
        result = _execute(config, prepared)
        code = result_exit_code(result)
        financial_code = result_exit_code(
            {key: value for key, value in result.items() if key != "backtest"}
        )
        if financial_code == 0:
            counts = result["counts"]
            print(
                "运行完成。"
                if result["status"] == "PUBLISHED"
                else "运行完成，输入未变化，已复用结果。"
            )
            print(
                f"财报记录：{counts['scan_returned_reports']}；因子位置：{counts['evaluated'] + counts['reused']}（包含有原因的缺值）。"
            )
            print(f"数据目录：{prepared['workspace']}")
            print(f"查看报告：{Path(result['export_path']) / 'report.html'}")
            if "backtest" in result:
                backtest = result["backtest"]
                print(f"回测状态：{backtest['status']}；总览：{backtest['report']}")
                if backtest.get("error"):
                    print(f"回测原因：{backtest['error']}")
                for item in backtest["results"]:
                    windows = item.get("summary", {}).get("formed_periods", 0)
                    print(
                        f"{item['factor']}：{item['status']}，有效窗口{windows}；{item.get('report', item.get('error', ''))}"
                    )
                if backtest["status"] != "ok":
                    print("财报结果已保留；检查回测输入与时间口径后重新运行入口。")
        else:
            print(f"运行未完成：{result['status']}；当前结果请先检查问题后使用。")
            if result.get("error"):
                print(f"原因：{result['error']}")
            if result.get("quality"):
                print(f"质量问题：{result['quality'].get('blocking_reasons', [])}")
            if result.get("scope_health"):
                print(f"数据状态：{result['scope_health']}")
            for name, label in (
                ("run_id", "运行编号"),
                ("capture_dir", "采集证据"),
            ):
                if result.get(name):
                    print(f"{label}：{result[name]}")
            if result.get("run_id") and result["status"] in ("FAILED", "BLOCKED", "WAITING_DATA"):
                print(f"恢复命令：{recovery_command(result['run_id'], prepared['workspace'])}")
        return code
    except Timeout:
        print("运行失败：数据目录正在被其他进程写入，请等待该运行结束。")
        return 4
    except (ValueError, OSError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(f"运行失败：{exc}")
        return 2
