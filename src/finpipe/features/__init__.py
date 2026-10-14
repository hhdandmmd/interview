"""财务特征入口；固定十五项与准确期间依赖，不引入动态计算框架。"""

from finpipe.features.calculation import (
    FEATURE_NAMES,
    compute_features,
    feature_dictionary,
    required_periods,
)

__all__ = ["FEATURE_NAMES", "compute_features", "feature_dictionary", "required_periods"]
