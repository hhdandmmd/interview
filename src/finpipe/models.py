"""公共数据契约：明确源、裁决、清洗和特征身份，金额统一为人民币元。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BusinessKey:
    """完整源业务键；报告日使用ISO日期，证券代码保留交易所。"""

    source: str
    security_code: str
    report_end: str
    statement_type: str
    consolidation_scope: str = "consolidated"


@dataclass(frozen=True)
class SourceRecord:
    """不可变来源内容及真实首次观察，raw保留原字段。"""

    source_version_id: str
    key: BusinessKey
    raw: dict[str, Any]
    first_seen_at: str


@dataclass(frozen=True)
class RecordState:
    """业务裁决事实；冲突和缺失也拥有非空实际证实时间。"""

    record_state_id: str
    key: BusinessKey
    state_status: str
    selected_source_version_id: str | None
    decision_observed_at: str
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CleanRecord:
    """精确清洗结果；字段状态与时间质量共同决定可计算性。"""

    clean_version_id: str
    source_version_id: str
    key: BusinessKey
    values: dict[str, float | None]
    field_states: dict[str, str]
    reasons: dict[str, tuple[str, ...]]
    quality_status: str
    source_available_at: str | None
    data_available_at: str
    processed_at: str
    time_valid: bool
    basis_status: str = "source_assumed"
    evidence_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeatureInput:
    """精确字段/期间角色；未满足输入保留业务键和失效证据。"""

    key: BusinessKey
    field_name: str
    input_role: str
    record_state_id: str | None
    clean_version_id: str | None
    input_status: str
    input_available_at: str
    source_available_at: str | None
    input_value: float | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeatureResult:
    """有限值或带明确原因的NULL，时间和血缘均冻结。"""

    feature_version_id: str
    security_code: str
    report_end: str
    feature_name: str
    feature_value: float | None
    feature_status: str
    storage_unit: str
    reason_codes: tuple[str, ...]
    evidence_flags: tuple[str, ...]
    data_available_at: str
    input_hash: str
    inputs: tuple[FeatureInput, ...]
