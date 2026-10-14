"""独立小型财报夹具验证质量分母、核对口径及基线边界。"""

import unittest
from dataclasses import replace

from finpipe.catalog import FIELDS
from finpipe.identity import stable_id
from finpipe.models import BusinessKey, FeatureInput, FeatureResult, RecordState, SourceRecord
from finpipe.quality import evaluate_quality
from finpipe.transforms import normalize

PROFILE = {
    "mapping_version": "1",
    "cleaning_rule_version": "1",
    "time_policy_version": "1",
    "formula_version": "1",
    "dictionary_version": "1",
    "parameters": {"amount_denominator": 1.0},
    "quality": {"min_usable_coverage": 0.95, "max_invalid_rate": 0.01},
}
OBSERVED = "2021-05-01T00:00:00.000000Z"


def fixture(code="600519.SH"):
    """四个同源核对的手算值为毛利40%、净利15%、负债40%、流动2倍。"""
    records, states, expected = [], [], []
    for table in ("income", "balance", "indicator"):
        key = BusinessKey("fixture", code, "2020-12-31", table)
        raw = {spec.source_field: 10 for spec in FIELDS[table]}
        raw.update(
            SECUCODE=code,
            CURRENCY="CNY",
            NOTICE_DATE="2021-04-01",
            UPDATE_DATE="2021-04-02",
            _evidence={"fixture": True, "consolidation_scope": "consolidated"},
        )
        if table == "income":
            raw.update(
                OPERATE_INCOME=300,
                TOTAL_OPERATE_INCOME=300,
                OPERATE_COST=180,
                NETPROFIT=45,
                PARENT_NETPROFIT=35,
            )
        elif table == "balance":
            raw.update(
                TOTAL_ASSETS=1000,
                TOTAL_LIABILITIES=400,
                TOTAL_EQUITY=600,
                TOTAL_PARENT_EQUITY=None,
                TOTAL_CURRENT_ASSETS=300,
                TOTAL_CURRENT_LIAB=150,
            )
        else:
            raw.update(ROEJQ=15, XSMLL=40, XSJLL=15, ZCFZL=40, LD=2)
        source_id = stable_id("source", raw)
        records.append(normalize(SourceRecord(source_id, key, raw, OBSERVED), PROFILE, OBSERVED))
        states.append(RecordState("state_" + source_id, key, "selected", source_id, OBSERVED))
        expected.append({"key": key, "eligibility": "eligible", "evidence": {"fixture": True}})
    return records, states, expected


class QualityTests(unittest.TestCase):
    """保守分母、局部错误与核对不能被条件百分比掩盖。"""

    def test_same_origin_four_matches_never_claim_independent_verification(self):
        records, states, expected = fixture()
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        self.assertTrue(actual["passed"])
        self.assertEqual(actual["metrics"]["usable_report_rate"], 1)
        self.assertEqual(actual["metrics"]["match_rate"], 1)
        self.assertEqual(len(actual["reconciliation"]), 4)
        self.assertTrue(all(item["result"] == "matched" for item in actual["reconciliation"]))
        self.assertTrue(
            all(item["evidence_tier"] == "same_origin_check" for item in actual["reconciliation"])
        )
        self.assertEqual(actual["metrics"]["baseline_status"], "baseline_insufficient")
        self.assertIsNone(actual["metrics"]["feature_value_coverage_rate"])

    def test_unknown_qualification_does_not_vanish_from_denominator(self):
        records, states, planned = [], [], []
        for company in range(1, 801):
            row_records, row_states, row_planned = fixture(f"{company:06d}.SH")
            if company <= 8:
                records.extend(row_records)
                states.extend(row_states)
            else:
                row_planned = [
                    {**item, "eligibility": "unknown", "evidence": {"fixture": "待核验"}}
                    for item in row_planned
                ]
            planned.extend(row_planned)
        actual = evaluate_quality(records, states, [], PROFILE, planned)
        self.assertFalse(actual["passed"])
        self.assertEqual(actual["metrics"]["planned_count"], 2400)
        self.assertEqual(actual["metrics"]["conditional_usable_rate"], 1)
        self.assertAlmostEqual(actual["metrics"]["usable_report_rate"], 0.01)
        self.assertAlmostEqual(actual["metrics"]["unknown_eligibility_rate"], 0.99)
        self.assertIn("usable_report_rate_below_budget", actual["blocking_reasons"])

    def test_unproven_exclusion_is_still_unknown(self):
        records, states, expected = fixture()
        expected.append(
            {
                "key": BusinessKey("fixture", "000333.SZ", "2020-12-31", "income"),
                "eligibility": "not_required",
                "evidence": {},
            }
        )
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        self.assertEqual(actual["metrics"]["planned_count"], 4)
        self.assertEqual(actual["metrics"]["unknown_count"], 1)
        self.assertAlmostEqual(actual["metrics"]["usable_report_rate"], 0.75)

    def test_zero_effective_scope_is_null_and_blocked(self):
        _, _, expected = fixture()
        expected = [{**item, "eligibility": "not_required"} for item in expected]
        actual = evaluate_quality([], [], [], PROFILE, expected)
        self.assertFalse(actual["passed"])
        self.assertIsNone(actual["metrics"]["usable_report_rate"])
        self.assertIsNone(actual["metrics"]["field_invalid_rate"])
        self.assertIn("zero_effective_scope", actual["blocking_reasons"])

    def test_duplicate_expected_business_key_blocks(self):
        records, states, expected = fixture()
        actual = evaluate_quality(records, states, [], PROFILE, expected + expected[:1])
        self.assertIn("duplicate_expected_key", actual["blocking_reasons"])
        self.assertEqual(actual["metrics"]["planned_count"], 3)

    def test_optional_null_does_not_make_core_balance_unusable(self):
        records, states, expected = fixture()
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        self.assertEqual(actual["metrics"]["usable_count"], 3)
        self.assertEqual(
            actual["metrics"]["field_rates"]["balance.parent_equity"]["missing_rate"], 1
        )

    def test_field_rate_uses_actual_received_applicable_population(self):
        records, states, expected = fixture()
        record = next(item for item in records if item.key.statement_type == "income")
        records = [
            replace(
                item,
                values={**item.values, "operating_cost_ytd": None},
                field_states={**item.field_states, "operating_cost_ytd": "invalid"},
            )
            if item == record
            else item
            for item in records
        ]
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        self.assertEqual(
            actual["metrics"]["field_rates"]["income.operating_cost_ytd"]["denominator"], 1
        )
        self.assertEqual(
            actual["metrics"]["field_rates"]["income.operating_cost_ytd"]["invalid_rate"], 1
        )
        self.assertEqual(actual["metrics"]["usable_count"], 2)
        self.assertIn("field_invalid_rate_above_budget", actual["blocking_reasons"])

    def test_local_conflict_is_budgeted_not_unconditionally_blocked(self):
        records, states, expected = fixture()
        balance = next(item for item in records if item.key.statement_type == "balance")
        states.append(
            RecordState("conflict", balance.key, "conflict", None, "2021-05-02T00:00:00.000000Z")
        )
        profile = {**PROFILE, "quality": {"min_usable_coverage": 0.6, "max_invalid_rate": 0.01}}
        actual = evaluate_quality(records, states, [], profile, expected)
        self.assertTrue(actual["passed"])
        self.assertEqual(actual["metrics"]["usable_count"], 2)
        self.assertTrue(
            any(item["rule_id"] == "source_version_conflict" for item in actual["events"])
        )

    def test_conflict_candidates_count_once_in_report_coverage(self):
        records, states, expected = fixture()
        balance = next(item for item in records if item.key.statement_type == "balance")
        records = [item for item in records if item != balance]
        states.append(
            RecordState(
                "conflict",
                balance.key,
                "conflict",
                None,
                "2021-05-02T00:00:00.000000Z",
                evidence={"candidate_source_version_ids": ["source_A", "source_B"]},
            )
        )
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        self.assertEqual(actual["metrics"]["report_coverage_rate"], 1)
        self.assertAlmostEqual(actual["metrics"]["usable_report_rate"], 2 / 3)
        self.assertEqual(actual["metrics"]["returned_candidate_count"], 4)
        self.assertEqual(actual["metrics"]["selected_record_count"], 2)

    def test_net_margin_different_total_revenue_is_not_comparable(self):
        records, states, expected = fixture()
        records = [
            replace(item, values={**item.values, "total_revenue_ytd": 310})
            if item.key.statement_type == "income"
            else item
            for item in records
        ]
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        net = next(
            item for item in actual["reconciliation"] if item["check_name"] == "net_margin_ytd"
        )
        self.assertEqual(net["result"], "not_comparable")
        self.assertIn("unverified_net_margin_denominator", net["reason_codes"])
        self.assertAlmostEqual(actual["metrics"]["comparison_coverage"], 0.75)
        self.assertEqual(actual["metrics"]["match_rate"], 1)

    def test_current_ratio_percent_mistake_is_mismatch_with_full_refs(self):
        records, states, expected = fixture()
        records = [
            replace(item, values={**item.values, "source_current_ratio": 200})
            if item.key.statement_type == "indicator"
            else item
            for item in records
        ]
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        current = next(
            item for item in actual["reconciliation"] if item["check_name"] == "current_ratio"
        )
        self.assertEqual(current["result"], "mismatched")
        self.assertEqual(current["absolute_difference"], 198)
        self.assertEqual(current["left_inputs"][0]["field_name"], "current_assets")
        self.assertIsNotNone(current["right_inputs"][0]["clean_version_id"])

    def test_both_missing_is_not_match(self):
        _, states, expected = fixture()
        states = [
            replace(item, state_status="missing", selected_source_version_id=None)
            for item in states
        ]
        actual = evaluate_quality([], states, [], PROFILE, expected)
        self.assertTrue(all(item["result"] == "both_missing" for item in actual["reconciliation"]))
        self.assertIsNone(actual["metrics"]["match_rate"])
        self.assertEqual(actual["metrics"]["comparison_coverage"], 0)

    def test_row_drop_and_mad_zero_are_explicit(self):
        records, states, expected = fixture()
        previous = {
            "metrics": {"received_count": 30, "field_null_rate": 1 / 16},
            "successful_row_counts": [30, 30, 30, 30, 30],
        }
        actual = evaluate_quality(records, states, [], PROFILE, expected, previous)
        self.assertIn("row_count_change_above_budget", actual["blocking_reasons"])
        self.assertEqual(actual["metrics"]["baseline_status"], "mad_zero")
        self.assertIsNone(actual["metrics"]["robust_row_score"])

    def test_balance_component_and_accounting_warnings_preserve_values(self):
        records, states, expected = fixture()
        records = [
            replace(item, values={**item.values, "current_assets": 1300, "total_equity": 500})
            if item.key.statement_type == "balance"
            else item
            for item in records
        ]
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        rules = {item["rule_id"] for item in actual["events"]}
        self.assertIn("accounting_identity_mismatch", rules)
        self.assertIn("balance_component_exceeds_total", rules)
        self.assertTrue(actual["passed"])

    def test_source_assumed_does_not_count_as_verified_quarter_basis(self):
        records, states, expected = fixture()
        current = next(item for item in records if item.key.statement_type == "income")
        prior = replace(
            current,
            clean_version_id="prior_clean",
            source_version_id="prior_source",
            key=replace(current.key, report_end="2020-09-30"),
        )
        inputs = (
            FeatureInput(
                current.key,
                "net_profit_ytd",
                "current",
                "state_current",
                current.clean_version_id,
                "satisfied",
                OBSERVED,
                current.source_available_at,
                45,
            ),
            FeatureInput(
                prior.key,
                "net_profit_ytd",
                "current.previous_ytd",
                "state_prior",
                prior.clean_version_id,
                "satisfied",
                OBSERVED,
                prior.source_available_at,
                30,
            ),
        )
        feature = FeatureResult(
            "feature",
            "600519.SH",
            "2020-12-31",
            "net_margin_q",
            0.15,
            "warning",
            "ratio",
            ("restatement_basis_unverified",),
            ("source_assumed_quarter",),
            OBSERVED,
            "input_fixture",
            inputs,
        )
        actual = evaluate_quality(records + [prior], states, [feature], PROFILE, expected)
        self.assertEqual(actual["metrics"]["quarter_derivation_key_count"], 1)
        self.assertEqual(actual["metrics"]["verified_basis_coverage_rate"], 0)
        records = [replace(item, basis_status="verified_comparable") for item in records + [prior]]
        verified = evaluate_quality(records, states, [feature], PROFILE, expected)
        self.assertEqual(verified["metrics"]["verified_basis_coverage_rate"], 1)

    def test_missing_reference_preserves_real_scan_state_and_time(self):
        records, states, expected = fixture()
        states = [
            replace(
                item,
                state_status="missing",
                selected_source_version_id=None,
                decision_observed_at="2021-05-03T00:00:00.000000Z",
            )
            if item.key.statement_type == "indicator"
            else item
            for item in states
        ]
        actual = evaluate_quality(records, states, [], PROFILE, expected)
        self.assertTrue(all(item["result"] == "missing_right" for item in actual["reconciliation"]))
        for item in actual["reconciliation"]:
            self.assertEqual(item["right_inputs"][0]["state_status"], "missing")
            self.assertEqual(
                item["right_inputs"][0]["data_available_at"], "2021-05-03T00:00:00.000000Z"
            )


if __name__ == "__main__":
    unittest.main()
