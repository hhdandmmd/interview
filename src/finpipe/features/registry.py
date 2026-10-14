"""显式登记因子，新增目录后只需在此加入；顺序稳定，不做目录扫描。"""

from finpipe.features.factors import (
    current_ratio,
    current_ratio_change_qoq,
    debt_asset_change_qoq_pp,
    debt_asset_ratio,
    gross_margin_q,
    gross_margin_ytd,
    net_margin_q,
    net_margin_ytd,
    parent_profit_growth_qoq,
    parent_profit_growth_yoy,
    revenue_growth_qoq,
    revenue_growth_yoy,
    roe_change_yoy,
    roe_change_yoy_pp,
    roe_weighted_ytd,
)

DEFINITIONS = (
    roe_weighted_ytd.FACTOR,
    roe_change_yoy.FACTOR,
    roe_change_yoy_pp.FACTOR,
    net_margin_q.FACTOR,
    gross_margin_q.FACTOR,
    net_margin_ytd.FACTOR,
    gross_margin_ytd.FACTOR,
    revenue_growth_yoy.FACTOR,
    revenue_growth_qoq.FACTOR,
    parent_profit_growth_yoy.FACTOR,
    parent_profit_growth_qoq.FACTOR,
    debt_asset_ratio.FACTOR,
    debt_asset_change_qoq_pp.FACTOR,
    current_ratio.FACTOR,
    current_ratio_change_qoq.FACTOR,
)
FACTORS = {factor.name: factor for factor in DEFINITIONS}
if len(FACTORS) != len(DEFINITIONS):
    raise ValueError("因子名称不能重复")
FEATURE_NAMES = tuple(FACTORS)
