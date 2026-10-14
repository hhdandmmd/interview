"""确定性身份只来自声明内容；运行时间不混入来源业务摘要。"""

import hashlib
import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


def canonical_json(value: Any) -> str:
    """生成稳定JSON；拒绝非有限数字，保留真正空值。"""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def stable_id(prefix: str, value: Any) -> str:
    """按规范内容生成身份，避免依赖字典序和代理ID判断新旧。"""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def source_number(value: Any) -> Any:
    """声明数值字段摘要规范化；非法原值留作证据，不强行清洗。"""
    if value is None or isinstance(value, bool):
        return value
    try:
        parsed = Decimal(str(value))
        if parsed.is_finite():
            return "0" if not parsed else format(parsed.normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        pass
    return str(value)
