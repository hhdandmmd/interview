# 本快照数据字典

- operating_revenue_ytd: 营业收入；单位 cny；期间 ytd
- total_revenue_ytd: 营业总收入；单位 cny；期间 ytd
- operating_cost_ytd: 营业成本；单位 cny；期间 ytd
- net_profit_ytd: 合并净利润；单位 cny；期间 ytd
- parent_net_profit_ytd: 归母净利润；单位 cny；期间 ytd
- total_assets: 总资产；单位 cny；期间 point_in_time
- total_liabilities: 总负债；单位 cny；期间 point_in_time
- total_equity: 权益合计；单位 cny；期间 point_in_time
- parent_equity: 归母权益；单位 cny；期间 point_in_time
- current_assets: 流动资产；单位 cny；期间 point_in_time
- current_liabilities: 流动负债；单位 cny；期间 point_in_time
- roe_weighted_ytd: 累计加权ROE；单位 ratio；期间 ytd
- source_gross_margin_ytd: 累计毛利率；单位 ratio；期间 ytd
- source_net_margin_ytd: 累计净利率；单位 ratio；期间 ytd
- source_debt_asset_ratio: 负债率；单位 ratio；期间 point_in_time
- source_current_ratio: 流动比率；单位 multiple；期间 point_in_time

## 财务特征

- roe_weighted_ytd: 披露累计加权ROE；ROE_ytd；单位 ratio
- roe_change_yoy: 披露ROE同比相对变化；ROE_ytd / lag4(ROE_ytd) - 1；单位 ratio
- roe_change_yoy_pp: 披露ROE同比百分点变化；ROE_ytd - lag4(ROE_ytd)；单位 ratio_delta
- net_margin_q: 单季合并净利率；NP_q / R_q；单位 ratio
- gross_margin_q: 单季毛利率；(R_q - C_q) / R_q；单位 ratio
- net_margin_ytd: 累计合并净利率；NP_ytd / R_ytd；单位 ratio
- gross_margin_ytd: 累计毛利率；(R_ytd - C_ytd) / R_ytd；单位 ratio
- revenue_growth_yoy: 单季营业收入同比；R_q / lag4(R_q) - 1；单位 ratio
- revenue_growth_qoq: 单季营业收入环比；R_q / lag1(R_q) - 1；单位 ratio
- parent_profit_growth_yoy: 单季归母利润同比；PNP_q / lag4(PNP_q) - 1；单位 ratio
- parent_profit_growth_qoq: 单季归母利润环比；PNP_q / lag1(PNP_q) - 1；单位 ratio
- debt_asset_ratio: 期末资产负债率；L / A；单位 ratio
- debt_asset_change_qoq_pp: 负债率环比百分点变化；L / A - lag1(L / A)；单位 ratio_delta
- current_ratio: 期末流动比率；CA / CL；单位 multiple
- current_ratio_change_qoq: 流动比率环比倍数变化；CA / CL - lag1(CA / CL)；单位 multiple_delta

## 持久表

- alerts: alert_id、event_id、scope_id、status、opened_at、resolved_at、recovery_run_id、payload_json
- batches: batch_id、run_id、status、content_json
- clean_balance: clean_version_id、total_assets、total_liabilities、total_equity、parent_equity、current_assets、current_liabilities
- clean_income: clean_version_id、operating_revenue_ytd、total_revenue_ytd、operating_cost_ytd、net_profit_ytd、parent_net_profit_ytd
- clean_indicator: clean_version_id、roe_weighted_ytd、source_gross_margin_ytd、source_net_margin_ytd、source_debt_asset_ratio、source_current_ratio
- clean_versions: clean_version_id、source_version_id、mapping_version、cleaning_rule_version、quality_status、payload_json
- dataset_snapshots: snapshot_id、scope_id、universe_id、profile_id、run_id、expected_parent_head、data_as_of、status、signature、manifest_hash、exported_at、published_at、publication_sequence
- expected_keys: scope_id、business_key、eligibility、evidence_json
- feature_inputs: feature_version_id、input_number、business_key、record_state_id、clean_version_id、field_name、input_role、input_available_at、payload_json
- feature_versions: feature_version_id、scope_id、profile_id、security_code、report_end、feature_name、feature_value、feature_status、data_available_at、computed_at、input_hash、payload_json
- http_attempts: attempt_id、batch_id、page_number、attempt_number、observed_at、body_path、raw_hash、content_json
- processing_profiles: profile_id、content_json
- quality_events: event_id、run_id、rule_id、severity、payload_json
- quality_metrics: run_id、metric_name、payload_json
- record_states: record_state_id、business_key、state_sequence、state_status、selected_source_version_id、decision_observed_at、payload_json
- run_inputs: run_id、observation_id
- runs: run_id、scope_id、profile_id、parent_run_id、status、phase、started_at、heartbeat_at、finished_at、plan_json、counts_json、snapshot_id、error_json
- schema_version: version
- scopes: scope_id、universe_id、capture_kind、content_json、health_status、health_reason、last_success_at
- snapshot_features: snapshot_id、security_code、report_end、feature_name、feature_version_id
- snapshot_records: snapshot_id、business_key、source、security_code、report_end、statement_type、consolidation_scope、record_state_id、selected_clean_version_id、data_available_at、source_available_at
- source_observations: observation_id、source_version_id、attempt_id、row_index、observed_at
- source_versions: source_version_id、business_key、source、security_code、report_end、statement_type、consolidation_scope、first_seen_at、raw_json
- universe_snapshots: universe_id、content_json
- workspace_head: scope_id、snapshot_id
