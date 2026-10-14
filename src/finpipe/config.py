"""读取配置并冻结业务口径；内部路径统一从绝对工作区推导。"""

import copy
import json
import re
import tomllib
from dataclasses import dataclass, field
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
class RunConfig:
    """日常运行只指定公司、来源和输出报告期；相对输出目录以调用目录为准。"""

    stocks: list[str] = field(default_factory=lambda: ["600519.SH"])
    source: str = "eastmoney"
    start_period: str = "2025Q1"
    end_period: str = "2025Q4"
    output_dir: str | Path = "output/financial"

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
        return load_config(
            values={
                "workspace": str(self.output_dir),
                "scope": {
                    "codes": self.stocks.copy(),
                    "input_start": f"{year - 1:04d}Q1",
                    "input_end": outputs[-1],
                    "output_start": outputs[0],
                    "output_end": outputs[-1],
                },
            }
        )
