"""读取配置并冻结业务口径；内部路径统一从绝对工作区推导。"""

import copy
import json
import math
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from finpipe.identity import stable_id
from finpipe.timeutil import quarters

DEFAULTS = {
    "workspace": "output/live",
    "scope": {
        "codes": ["600519.SH", "000333.SZ", "000651.SZ"],
        "input_start": "2022Q1",
        "input_end": "2025Q4",
        "output_start": "2023Q1",
        "output_end": "2025Q4",
    },
    "network": {"interval": 1.0, "timeout": 30.0, "max_attempts": 3, "max_wait": 30.0},
    "quality": {"min_usable_coverage": 0.95, "max_invalid_rate": 0.01},
    "job": {"period_seconds": 86400, "grace_seconds": 900, "max_run_seconds": 7200},
    "profile": {
        "mapping_version": "1",
        "cleaning_rule_version": "1",
        "formula_version": "2",
        "source_selection_policy": "latest_complete_observation",
        "dictionary_version": "1",
        "time_policy_version": "1",
        "parameters": {"amount_denominator": 1.0, "ratio_denominator": 1e-8},
    },
}


def load_config(
    path: Path | None = None, workspace: Path | None = None, *, values: dict | None = None
) -> dict:
    """文件或内存配置二选一；共用口径校验，未知键不静默忽略。"""
    if path is not None and values is not None:
        raise ValueError("配置文件与内存配置不能同时指定")
    base = Path.cwd()
    incoming = {} if values is None else values
    if path:
        path = path.resolve()
        base = path.parent
        incoming = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(incoming, dict):
        raise ValueError("配置必须为对象")
    if "scope" in incoming and not isinstance(incoming["scope"], dict):
        raise ValueError("配置段或字段无效：scope")
    result = copy.deepcopy(DEFAULTS)
    # 未指定公司时才加载冻结名单；简单入口的自选公司不被默认50家公司覆盖。
    if "codes" not in incoming.get("scope", {}):
        result["scope"]["codes"] = json.loads(
            (Path(__file__).parent / "resources/demo/universe/universe.json").read_text(
                encoding="utf-8"
            )
        )["codes"]
    for key, value in incoming.items():
        if key not in DEFAULTS:
            raise ValueError(f"未知配置键：{key}")
        if isinstance(result[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"配置段或字段无效：{key}")
            unknown = set(value) - set(result[key])
            if unknown:
                raise ValueError(f"未知配置字段：{key}." + f", {key}.".join(sorted(unknown)))
            result[key].update(value)
        else:
            result[key] = value
    result["_output_explicit"] = "output_start" in incoming.get("scope", {})
    if not isinstance(result["workspace"], str) or not result["workspace"].strip():
        raise ValueError("workspace必须为非空目录字符串")
    requested = workspace if workspace is not None else Path(result["workspace"])
    workspace_base = Path.cwd() if workspace is not None else base
    result["workspace"] = str(
        (workspace_base / requested).resolve()
        if not requested.is_absolute()
        else requested.resolve()
    )
    scope = result["scope"]
    codes = scope["codes"]
    if not isinstance(codes, list) or not codes or any(not isinstance(code, str) for code in codes):
        raise ValueError("公司名单必须为非空且不重复的字符串列表")
    if len(set(codes)) != len(codes):
        raise ValueError("公司名单不能重复")
    if any(not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code) for code in codes):
        raise ValueError("公司代码必须包含六位代码及SH/SZ/BJ交易所")
    for name in ("input_start", "input_end", "output_start", "output_end"):
        if not isinstance(scope[name], str):
            raise ValueError(f"scope.{name}必须为季度字符串，如2023Q1")
    inputs = quarters(scope["input_start"], scope["input_end"])
    outputs = quarters(scope["output_start"], scope["output_end"])
    if not set(outputs) <= set(inputs):
        raise ValueError("输出报告期必须包含在输入范围中")
    first_year = int(outputs[0][:4])
    if inputs[0] > f"{first_year - 1}-03-31":
        raise ValueError("输入缺少同比及单季所需的一年上下文")
    for key in ("interval", "timeout", "max_wait"):
        value = result["network"][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 3600:
            raise ValueError(f"网络参数无效：{key}")
    if (
        type(result["network"]["max_attempts"]) is not int
        or not 1 <= result["network"]["max_attempts"] <= 5
    ):
        raise ValueError("重试次数须为1至5的整数")
    for key, value in result["job"].items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"调度时间须为正整数秒：{key}")
    for key in ("min_usable_coverage", "max_invalid_rate"):
        value = result["quality"][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"质量预算无效：{key}")
    for name in ("mapping_version", "formula_version", "dictionary_version", "time_policy_version"):
        if result["profile"][name] != ("2" if name == "formula_version" else "1"):
            raise ValueError(f"当前实现不支持该版本：{name}")
    if result["profile"]["source_selection_policy"] != "latest_complete_observation":
        raise ValueError("新运行仅支持完整观察快照政策；旧政策只能按冻结计划恢复")
    label = result["profile"]["cleaning_rule_version"]
    if not isinstance(label, str) or not label.strip():
        raise ValueError("清洗规则版本标签必须为非空字符串")
    parameters = result["profile"]["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != {
        "amount_denominator",
        "ratio_denominator",
    }:
        raise ValueError("计算参数集合不完整或包含未知键")
    for value in parameters.values():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 < value < 1e12:
            raise ValueError("分母容差须为有限正数")
    result["profile"]["cleaning_rule_label"] = result["profile"]["cleaning_rule_version"]
    result["profile"]["cleaning_rule_version"] = stable_id(
        "rule",
        {
            "label": result["profile"]["cleaning_rule_version"],
            "parameters": parameters,
        },
    )
    result["profile"]["quality"] = result["quality"]
    result["profile_id"] = stable_id("profile", result["profile"])
    result["input_periods"], result["output_periods"] = inputs, outputs
    return result


@dataclass
class BacktestConfig:
    """可选的独立因子分组研究；日期是交易区间，和财报报告期分别维护。"""

    enabled: bool = False
    factors: list[str] = field(default_factory=lambda: ["roe_weighted_ytd"])
    groups: int = 5
    horizon: int = 20
    cost_bps: float = 0.0
    time_policy: str = "observed"
    start_date: str | None = None
    end_date: str | None = None
    market_dir: str | Path | None = None

    def parameters(self, stocks: list[str], periods: tuple[str, ...]) -> dict | None:
        """启用时在财报采集前校验；未提供行情目录则由现有腾讯入口自动采集。"""
        if type(self.enabled) is not bool:
            raise ValueError("backtest.enabled须为True或False")
        if not self.enabled:
            return None
        from finpipe.features import FEATURE_NAMES

        if (
            not isinstance(self.factors, list)
            or not self.factors
            or any(not isinstance(name, str) for name in self.factors)
            or len(set(self.factors)) != len(self.factors)
        ):
            raise ValueError("backtest.factors须为非空且不重复的因子名称列表")
        unknown = set(self.factors) - set(FEATURE_NAMES)
        if unknown:
            raise ValueError(f"未知回测因子：{', '.join(sorted(unknown))}")
        if type(self.groups) is not int or not 2 <= self.groups <= 20:
            raise ValueError("backtest.groups须为2至20的整数")
        if len(stocks) < self.groups:
            raise ValueError(
                f"{self.groups}组回测至少需要{self.groups}只股票，请调整stocks或groups"
            )
        if type(self.horizon) is not int or self.horizon < 1:
            raise ValueError("backtest.horizon须为正整数交易日数")
        if (
            isinstance(self.cost_bps, bool)
            or not isinstance(self.cost_bps, (int, float))
            or not math.isfinite(self.cost_bps)
            or not 0 <= self.cost_bps < 10000
        ):
            raise ValueError("backtest.cost_bps须为0至10000之间的有限单边基点数")
        if self.time_policy not in ("observed", "publication_proxy"):
            raise ValueError("backtest.time_policy须为observed或publication_proxy")
        start = self.start_date if self.start_date is not None else periods[0][:4] + "-01-01"
        end = self.end_date if self.end_date is not None else periods[-1]
        for value in (start, end):
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("回测起止日期须为准确ISO日期，如2025-01-01")
        if start > end:
            raise ValueError("回测起始日期不能晚于结束日期")
        if self.market_dir is not None and (
            not isinstance(self.market_dir, (str, Path)) or not str(self.market_dir).strip()
        ):
            raise ValueError("backtest.market_dir须为行情目录或None")
        if self.market_dir is None:
            if len(stocks) > 50 or any(
                code.endswith(".BJ") or code == "000001.SH" for code in stocks
            ):
                raise ValueError("自动行情支持至多50只沪深股票；其他范围请指定backtest.market_dir")
            if int(end[:4]) - int(start[:4]) > 4:
                raise ValueError("自动行情至多跨五个自然年；更长范围请指定backtest.market_dir")
        return {
            "factors": self.factors.copy(),
            "groups": self.groups,
            "horizon": self.horizon,
            "cost_bps": self.cost_bps,
            "time_policy": self.time_policy,
            "start": start,
            "end": end,
            "market_dir": str(Path(self.market_dir).resolve())
            if self.market_dir is not None
            else None,
        }


@dataclass
class RunConfig:
    """日常运行只指定公司、来源和输出报告期；相对输出目录以调用目录为准。"""

    stocks: list[str] = field(default_factory=lambda: ["600519.SH"])
    source: str = "eastmoney"
    start_period: str = "2025Q1"
    end_period: str = "2025Q4"
    output_dir: str | Path = "output/financial"
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    def to_pipeline_config(self) -> dict:
        """自动补齐上一自然年Q1起的历史，供单季、同比和跨年环比计算。"""
        if self.source not in ("eastmoney", "东方财富"):
            raise ValueError("当前数据来源仅支持东方财富（eastmoney），其他网站尚未接入")
        if not isinstance(self.stocks, list):
            raise ValueError('stocks须为股票代码列表，如["600519.SH"]')
        if not isinstance(self.start_period, str) or not isinstance(self.end_period, str):
            raise ValueError("报告期须为季度字符串，如2025Q1")
        if not isinstance(self.output_dir, (str, Path)) or not str(self.output_dir).strip():
            raise ValueError("output_dir须为非空目录")
        outputs = quarters(self.start_period, self.end_period)
        year = int(outputs[0][:4])
        if year <= 1:
            raise ValueError("起始报告期须留出上一年的比较历史")
        from finpipe.features import required_periods

        # 标准因子保留上一整年；新增更长滞后因子时按声明自动向前补历史。
        first_required = required_periods(outputs)[0]
        input_start = (
            f"{year - 1:04d}Q1" if first_required >= f"{year - 1:04d}-03-31" else first_required
        )
        prepared = load_config(
            values={
                "workspace": str(self.output_dir),
                "scope": {
                    "codes": self.stocks.copy(),
                    "input_start": input_start,
                    "input_end": outputs[-1],
                    "output_start": outputs[0],
                    "output_end": outputs[-1],
                },
            }
        )
        if not isinstance(self.backtest, BacktestConfig):
            raise ValueError("backtest须为BacktestConfig")
        self.backtest.parameters(prepared["scope"]["codes"], prepared["output_periods"])
        return prepared
