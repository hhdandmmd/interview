"""人工财务夹具的独立手算与状态边界，不使用计算实现生成预期值。"""

import copy
import unittest
from dataclasses import replace

from finpipe.catalog import FIELDS
from finpipe.features import FEATURE_NAMES, compute_features, feature_dictionary, required_periods
from finpipe.identity import stable_id
from finpipe.models import BusinessKey, RecordState, SourceRecord
from finpipe.transforms import normalize

PROFILE = {
    "mapping_version": "1",
    "cleaning_rule_version": "1",
    "time_policy_version": "1",
    "formula_version": "1",
    "dictionary_version": "1",
}
CODE = "600519.SH"
OBSERVED = "2021-05-01T00:00:00.000000Z"
DECIDED = "2021-05-01T00:00:01.000000Z"
CUTOFF = "2021-06-01T00:00:00.000000Z"


def fixture() -> tuple[list, list]:
    """金额为元；2019/2020各八项累计与期末数据均属人工样本。"""
    amounts = {
        2019: ([100, 240, 390, 600], [60, 150, 240, 360], [10, 24, 39, 60], [8, 20, 32, 50]),
        2020: ([120, 300, 500, 800], [72, 174, 294, 474], [18, 45, 75, 120], [12, 36, 64, 112]),
    }
    records, states = [], []
    for year in (2019, 2020):
        for quarter, suffix in enumerate(("03-31", "06-30", "09-30", "12-31")):
            period = f"{year}-{suffix}"
            for table in ("income", "balance", "indicator"):
                key = BusinessKey("fixture", CODE, period, table)
                raw = {
                    "SECUCODE": CODE,
                    "REPORT_DATE": period,
                    "CURRENCY": "CNY",
                    "NOTICE_DATE": f"{year + 1}-04-01",
                    "UPDATE_DATE": f"{year + 1}-04-02",
                    "_evidence": {
                        "fixture": True,
                        "consolidation_scope": "consolidated",
                        "disclosed_precision": "date",
                        "updated_precision": "date",
                    },
                }
                raw.update({spec.source_field: 0.0 for spec in FIELDS[table]})
                if table == "income":
                    revenue, cost, net, parent = (values[quarter] for values in amounts[year])
                    raw.update(
                        OPERATE_INCOME=revenue,
                        TOTAL_OPERATE_INCOME=revenue,
                        OPERATE_COST=cost,
                        NETPROFIT=net,
                        PARENT_NETPROFIT=parent,
                    )
                elif table == "balance":
                    liabilities = 450 + quarter * 50 if year == 2020 else 400 + quarter * 30
                    raw.update(
                        TOTAL_ASSETS=1000,
                        TOTAL_LIABILITIES=liabilities,
                        TOTAL_EQUITY=1000 - liabilities,
                        TOTAL_PARENT_EQUITY=300,
                        TOTAL_CURRENT_ASSETS=270 + quarter * 30,
                        TOTAL_CURRENT_LIAB=150,
                    )
                else:
                    raw.update(ROEJQ=(6 + quarter * 6) if year == 2020 else (5 + quarter * 5), LD=2)
                source_id = stable_id("source", raw)
                record = normalize(SourceRecord(source_id, key, raw, OBSERVED), PROFILE, DECIDED)
                records.append(record)
                states.append(
                    RecordState(
                        stable_id("state", {"key": key.__dict__, "source": source_id}),
                        key,
                        "selected",
                        source_id,
                        DECIDED,
                    )
                )
    return records, states


def calculate(records, states, period="2020-09-30", **kwargs):
    """仅用于缩短夹具调用，不参与预期数值的手算。"""
    return {
        item.feature_name: item
        for item in compute_features(
            records,
            states,
            (CODE,),
            (period,),
            kwargs.pop("data_as_of", CUTOFF),
            kwargs.pop("profile_id", "profile_fixture"),
            "scope_fixture",
            **kwargs,
        )
    }


class FinanceTests(unittest.TestCase):
    """明确核验十五公式、时间、局部错误、失效与重述传播。"""

    def test_all_fifteen_independent_values(self):
        records, states = fixture()
        actual = calculate(records, states)
        expected = {
            "roe_weighted_ytd": 0.18,
            "roe_change_yoy": 0.2,
            "roe_change_yoy_pp": 0.03,
            "net_margin_q": 0.15,
            "gross_margin_q": 0.4,
            "net_margin_ytd": 0.15,
            "gross_margin_ytd": 0.412,
            "revenue_growth_yoy": 1 / 3,
            "revenue_growth_qoq": 1 / 9,
            "parent_profit_growth_yoy": 4 / 3,
            "parent_profit_growth_qoq": 1 / 6,
            "debt_asset_ratio": 0.55,
            "debt_asset_change_qoq_pp": 0.05,
            "current_ratio": 2.2,
            "current_ratio_change_qoq": 0.2,
        }
        self.assertEqual(set(actual), set(FEATURE_NAMES))
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertAlmostEqual(actual[name].feature_value, value)
                self.assertIn(actual[name].feature_status, ("ok", "warning"))
                self.assertEqual(actual[name].data_available_at, DECIDED)
        self.assertEqual(actual["net_margin_q"].feature_status, "warning")
        self.assertIn("source_assumed_quarter", actual["net_margin_q"].evidence_flags)
        self.assertEqual(actual["current_ratio"].storage_unit, "multiple")
        self.assertEqual(actual["roe_change_yoy_pp"].storage_unit, "ratio_delta")

    def test_cross_year_q1_qoq_uses_prior_q4_difference(self):
        records, states = fixture()
        actual = calculate(records, states, "2020-03-31")
        self.assertAlmostEqual(actual["revenue_growth_qoq"].feature_value, 120 / 210 - 1)
        self.assertAlmostEqual(actual["parent_profit_growth_qoq"].feature_value, 12 / 18 - 1)
        self.assertAlmostEqual(actual["revenue_growth_yoy"].feature_value, 0.2)
        periods = {item.key.report_end for item in actual["revenue_growth_qoq"].inputs}
        self.assertEqual(periods, {"2020-03-31", "2019-12-31", "2019-09-30"})

    def test_field_missing_does_not_clear_unrelated_factor(self):
        records, states = fixture()
        current = next(
            item
            for item in records
            if item.key.statement_type == "income" and item.key.report_end == "2020-09-30"
        )
        replaced = replace(
            current,
            values={**current.values, "operating_cost_ytd": None},
            field_states={**current.field_states, "operating_cost_ytd": "missing"},
            reasons={**current.reasons, "operating_cost_ytd": ("source_null",)},
        )
        records = [replaced if item == current else item for item in records]
        actual = calculate(records, states)
        self.assertIsNone(actual["gross_margin_q"].feature_value)
        self.assertEqual(actual["gross_margin_q"].feature_status, "missing_input")
        self.assertAlmostEqual(actual["net_margin_q"].feature_value, 0.15)
        self.assertAlmostEqual(actual["debt_asset_ratio"].feature_value, 0.55)

    def test_new_bad_state_never_resurrects_old_240(self):
        records, states = fixture()
        prior = next(
            item
            for item in records
            if item.key.statement_type == "income" and item.key.report_end == "2019-06-30"
        )
        bad = replace(
            prior,
            source_version_id="changed_source",
            clean_version_id="changed_clean",
            values={**prior.values, "operating_revenue_ytd": None},
            field_states={**prior.field_states, "operating_revenue_ytd": "invalid"},
            reasons={**prior.reasons, "operating_revenue_ytd": ("nonfinite_number",)},
            data_available_at="2021-05-02T00:00:00.000000Z",
        )
        records.append(bad)
        states.append(
            RecordState(
                "bad_state", bad.key, "selected", bad.source_version_id, bad.data_available_at
            )
        )
        actual = calculate(records, states)
        self.assertEqual(actual["revenue_growth_yoy"].feature_status, "input_error")
        self.assertIsNone(actual["revenue_growth_yoy"].feature_value)
        self.assertIn(
            "bad_state", {item.record_state_id for item in actual["revenue_growth_yoy"].inputs}
        )
        self.assertAlmostEqual(actual["net_margin_q"].feature_value, 0.15)

    def test_missing_exact_quarter_is_not_previous_existing_row(self):
        records, states = fixture()
        key = BusinessKey("fixture", CODE, "2020-06-30", "income")
        states.append(
            RecordState("missing_q2", key, "missing", None, "2021-05-03T00:00:00.000000Z")
        )
        actual = calculate(records, states)
        self.assertIsNone(actual["net_margin_q"].feature_value)
        self.assertEqual(actual["net_margin_q"].feature_status, "missing_history")
        self.assertAlmostEqual(actual["net_margin_ytd"].feature_value, 0.15)
        self.assertEqual(actual["net_margin_q"].data_available_at, "2021-05-03T00:00:00.000000Z")

    def test_conflict_priority_and_recovery_keep_both_histories(self):
        records, states = fixture()
        record = next(
            item
            for item in records
            if item.key.statement_type == "balance" and item.key.report_end == "2020-09-30"
        )
        states.append(
            RecordState("conflict", record.key, "conflict", None, "2021-05-03T00:00:00.000000Z")
        )
        conflicted = calculate(records, states)
        self.assertEqual(conflicted["debt_asset_ratio"].feature_status, "version_conflict")
        self.assertIsNone(conflicted["debt_asset_ratio"].feature_value)
        states.append(
            RecordState(
                "recovered",
                record.key,
                "selected",
                record.source_version_id,
                "2021-05-04T00:00:00.000000Z",
            )
        )
        recovered = calculate(records, states)
        self.assertAlmostEqual(recovered["debt_asset_ratio"].feature_value, 0.55)
        self.assertNotEqual(
            recovered["debt_asset_ratio"].feature_version_id,
            conflicted["debt_asset_ratio"].feature_version_id,
        )

    def test_known_restatement_200_250_380_requires_comparable_pair(self):
        records, states = fixture()
        for period, amount in (("2020-06-30", 200), ("2020-09-30", 380)):
            records = [
                replace(
                    item,
                    values={**item.values, "operating_revenue_ytd": amount},
                    basis_status="known_risk_unresolved",
                )
                if item.key.statement_type == "income" and item.key.report_end == period
                else item
                for item in records
            ]
        blocked = calculate(records, states)
        self.assertEqual(blocked["revenue_growth_qoq"].feature_status, "input_error")
        self.assertIsNone(blocked["gross_margin_q"].feature_value)
        self.assertAlmostEqual(blocked["net_margin_ytd"].feature_value, 75 / 380)
        for period, amount in (("2020-03-31", 120), ("2020-06-30", 250), ("2020-09-30", 380)):
            records = [
                replace(
                    item,
                    values={**item.values, "operating_revenue_ytd": amount},
                    basis_status="verified_comparable",
                )
                if item.key.statement_type == "income" and item.key.report_end == period
                else item
                for item in records
            ]
        restored = calculate(records, states)
        self.assertAlmostEqual(restored["revenue_growth_qoq"].feature_value, 0)
        self.assertAlmostEqual(restored["net_margin_q"].feature_value, 30 / 130)

    def test_negative_roe_growth_is_null_but_pp_is_valid(self):
        records, states = fixture()
        records = [
            replace(item, values={**item.values, "roe_weighted_ytd": -0.15})
            if item.key.statement_type == "indicator" and item.key.report_end == "2019-09-30"
            else item
            for item in records
        ]
        actual = calculate(records, states)
        self.assertEqual(actual["roe_change_yoy"].feature_status, "nonpositive_baseline")
        self.assertIsNone(actual["roe_change_yoy"].feature_value)
        self.assertAlmostEqual(actual["roe_change_yoy_pp"].feature_value, 0.33)
        self.assertIn("turn_profit", actual["roe_change_yoy"].evidence_flags)

    def test_tiny_positive_roe_base_is_not_nonpositive(self):
        records, states = fixture()
        records = [
            replace(item, values={**item.values, "roe_weighted_ytd": 1e-9})
            if item.key.statement_type == "indicator" and item.key.report_end == "2019-09-30"
            else item
            for item in records
        ]
        actual = calculate(records, states)
        self.assertEqual(actual["roe_change_yoy"].feature_status, "invalid_denominator")
        self.assertIn("small_positive_denominator", actual["roe_change_yoy"].reason_codes)

    def test_turn_loss_is_valid_with_positive_profit_baseline(self):
        records, states = fixture()
        records = [
            replace(item, values={**item.values, "parent_net_profit_ytd": 20})
            if item.key.statement_type == "income" and item.key.report_end == "2020-09-30"
            else item
            for item in records
        ]
        actual = calculate(records, states)
        self.assertAlmostEqual(actual["parent_profit_growth_yoy"].feature_value, -16 / 12 - 1)
        self.assertIn("turn_loss", actual["parent_profit_growth_yoy"].evidence_flags)

    def test_bad_notice_invalidates_income_only_with_nonempty_boundary(self):
        records, states = fixture()
        records = [
            replace(
                item,
                time_valid=False,
                source_available_at=None,
                reasons={**item.reasons, "__time__": ("missing_disclosure_evidence",)},
            )
            if item.key.statement_type == "income" and item.key.report_end == "2020-09-30"
            else item
            for item in records
        ]
        actual = calculate(records, states)
        self.assertEqual(actual["net_margin_ytd"].feature_status, "input_error")
        self.assertIsNone(actual["net_margin_ytd"].feature_value)
        self.assertEqual(actual["net_margin_ytd"].data_available_at, DECIDED)
        self.assertAlmostEqual(actual["debt_asset_ratio"].feature_value, 0.55)

    def test_all_missing_with_real_scan_retains_fifteen_null_cells(self):
        records, states = fixture()
        states = [
            RecordState(item.record_state_id + "_missing", item.key, "missing", None, DECIDED)
            for item in states
        ]
        actual = calculate([], states)
        self.assertEqual(len(actual), 15)
        self.assertTrue(all(item.feature_value is None for item in actual.values()))
        self.assertTrue(all(item.data_available_at == DECIDED for item in actual.values()))

    def test_nonpositive_profit_base_preserves_diagnostic(self):
        records, states = fixture()
        records = [
            replace(item, values={**item.values, "parent_net_profit_ytd": 19})
            if item.key.statement_type == "income" and item.key.report_end == "2019-09-30"
            else item
            for item in records
        ]
        actual = calculate(records, states)
        self.assertEqual(actual["parent_profit_growth_yoy"].feature_status, "nonpositive_baseline")
        self.assertIn("turn_profit", actual["parent_profit_growth_yoy"].evidence_flags)

    def test_repeat_evaluation_cutoff_is_not_identity(self):
        records, states = fixture()
        first, later = (
            calculate(records, states),
            calculate(records, states, data_as_of="2022-06-01"),
        )
        self.assertEqual(
            {name: item.feature_version_id for name, item in first.items()},
            {name: item.feature_version_id for name, item in later.items()},
        )

    def test_missing_scan_cannot_be_fabricated_from_cutoff(self):
        records, states = fixture()
        states = [
            item
            for item in states
            if not (item.key.statement_type == "income" and item.key.report_end == "2020-06-30")
        ]
        with self.assertRaisesRegex(ValueError, "扫描证据"):
            calculate(records, states)

    def test_required_periods_cover_cross_year_inputs(self):
        self.assertEqual(
            required_periods(("2020-03-31",)),
            ("2019-03-31", "2019-09-30", "2019-12-31", "2020-03-31"),
        )

    def test_dictionary_units_and_dependencies_match_actual_features(self):
        records, states = fixture()
        results = calculate(records, states)
        entries = feature_dictionary()
        self.assertEqual([item["name"] for item in entries], list(FEATURE_NAMES))
        for item in entries:
            self.assertEqual(item["storage_unit"], results[item["name"]].storage_unit)
            self.assertTrue(item["null_policy"])
            self.assertTrue(item["period_dependencies"])
        roe = next(item for item in entries if item["name"] == "roe_change_yoy")
        self.assertEqual(roe["period_dependencies"][0]["period_basis"], "ytd")
        self.assertEqual(roe["period_dependencies"][0]["lags"], [0, 4])
        profit = next(item for item in entries if item["name"] == "parent_profit_growth_qoq")
        self.assertEqual(profit["period_dependencies"][0]["field_name"], "parent_net_profit_ytd")
        self.assertEqual(profit["period_dependencies"][0]["lags"], [0, 1])


class NormalizeTests(unittest.TestCase):
    """原始字段类型、币种、合并范围和日期精度的人工边界样本。"""

    def raw(self):
        raw = {spec.source_field: 10 for spec in FIELDS["income"]}
        raw.update(
            CURRENCY="CNY",
            NOTICE_DATE="2020-04-01",
            UPDATE_DATE="2020-04-02",
            _evidence={"consolidation_scope": "consolidated", "fixture": True},
        )
        return raw

    def clean(self, raw, observed=OBSERVED):
        return normalize(
            SourceRecord(
                "fixture_source",
                BusinessKey("fixture", CODE, "2019-12-31", "income"),
                raw,
                observed,
            ),
            PROFILE,
            DECIDED,
        )

    def test_local_bool_nonfinite_invalid_and_loss_kept(self):
        raw = self.raw()
        raw.update(OPERATE_INCOME=True, OPERATE_COST="Infinity", NETPROFIT=-20)
        actual = self.clean(raw)
        self.assertIsNone(actual.values["operating_revenue_ytd"])
        self.assertIsNone(actual.values["operating_cost_ytd"])
        self.assertEqual(actual.values["net_profit_ytd"], -20)
        self.assertEqual(actual.quality_status, "accepted_with_issues")

    def test_notice_bad_is_not_replaced_by_update(self):
        for notice in (None, "坏日期", "2019-01-01", "2099-01-01"):
            with self.subTest(notice=notice):
                raw = self.raw()
                raw["NOTICE_DATE"] = notice
                actual = self.clean(raw)
                self.assertFalse(actual.time_valid)
                self.assertIsNone(actual.source_available_at)
                self.assertEqual(actual.data_available_at, OBSERVED)

    def test_missing_update_warns_and_date_projects_next_day(self):
        raw = self.raw()
        raw["UPDATE_DATE"] = None
        actual = self.clean(raw)
        self.assertTrue(actual.time_valid)
        self.assertEqual(actual.source_available_at, "2020-04-01T16:00:00.000000Z")
        self.assertIn("missing_update_evidence", actual.reasons["__time__"])

    def test_same_observation_day_projection_is_not_source_future(self):
        raw = self.raw()
        raw.update(NOTICE_DATE="2020-04-01", UPDATE_DATE=None)
        actual = self.clean(raw, "2020-04-01T03:00:00.000000Z")
        self.assertTrue(actual.time_valid)
        self.assertEqual(actual.data_available_at, "2020-04-01T16:00:00.000000Z")

    def test_precise_future_timestamp_and_bad_update_fail_closed(self):
        raw = self.raw()
        raw["_evidence"]["updated_precision"] = "timestamp"
        raw["UPDATE_DATE"] = "2021-05-01T01:00:00+00:00"
        self.assertFalse(self.clean(raw).time_valid)
        raw["UPDATE_DATE"] = "garbage"
        self.assertFalse(self.clean(raw).time_valid)

    def test_source_spec_currency_assumption_is_explicit(self):
        raw = self.raw()
        raw.pop("CURRENCY")
        self.assertEqual(self.clean(raw).quality_status, "quarantined")
        raw["_evidence"].update(currency_assumed="CNY", currency_evidence="documented_source_spec")
        actual = self.clean(raw)
        self.assertEqual(actual.quality_status, "accepted_with_issues")
        self.assertIn("currency_source_spec_assumed", actual.evidence_flags)
        raw["CURRENCY"] = "USD"
        self.assertEqual(self.clean(raw).quality_status, "quarantined")

    def test_unknown_scope_quarantines_and_input_immutable(self):
        raw = self.raw()
        raw["_evidence"]["consolidation_scope"] = "unknown"
        before = copy.deepcopy(raw)
        self.assertEqual(self.clean(raw).quality_status, "quarantined")
        self.assertEqual(before, raw)

    def test_unverified_basis_claim_is_not_trusted(self):
        raw = self.raw()
        raw["_basis_status"] = "verified_comparable"
        actual = self.clean(raw)
        self.assertEqual(actual.basis_status, "source_assumed")
        raw["_evidence"]["basis_evidence"] = "fixture_period_pair_reference"
        self.assertEqual(self.clean(raw).basis_status, "verified_comparable")

    def test_unknown_time_policy_is_rejected(self):
        source = SourceRecord(
            "fixture_source",
            BusinessKey("fixture", CODE, "2019-12-31", "income"),
            self.raw(),
            OBSERVED,
        )
        with self.assertRaisesRegex(ValueError, "时间政策"):
            normalize(source, {**PROFILE, "time_policy_version": "2"}, DECIDED)

    def test_quarantine_boundary_does_not_wait_for_next_day(self):
        raw = self.raw()
        raw.update(CURRENCY="USD", NOTICE_DATE="2020-04-01", UPDATE_DATE=None)
        actual = self.clean(raw, "2020-04-01T03:00:00.000000Z")
        self.assertEqual(actual.quality_status, "quarantined")
        self.assertEqual(actual.data_available_at, "2020-04-01T03:00:00.000000Z")
        self.assertEqual(actual.source_available_at, "2020-04-01T16:00:00.000000Z")

    def test_date_only_is_not_an_actual_observation_timestamp(self):
        with self.assertRaisesRegex(ValueError, "真实观察"):
            self.clean(self.raw(), "2021-05-01")


if __name__ == "__main__":
    unittest.main()
