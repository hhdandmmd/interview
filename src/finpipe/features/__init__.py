"""财务特征入口；因子独立维护，计算与字典使用同一显式登记。"""

from finpipe.features.calculation import (
    compute_features,
    required_periods,
)
from finpipe.features.registry import FEATURE_NAMES
from finpipe.features.specifications import feature_dictionary

__all__ = ["FEATURE_NAMES", "compute_features", "feature_dictionary", "required_periods"]
