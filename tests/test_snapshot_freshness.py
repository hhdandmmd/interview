"""新增来源事实使旧候选过期；人工夹具专门覆盖head尚未改变的恢复边界。"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fixtures.financial_cases import write_manifest

from finpipe.config import load_config
from finpipe.pipeline import execute_manifest, resume_run
from finpipe.storage import Store


def test_overlapping_scope_does_not_consume_old_values_after_a_new_bad_source():
    """两个范围共享来源时，另一个范围的新坏状态也使旧头的当前消费失效。"""
    with tempfile.TemporaryDirectory(prefix="finpipe 人工跨范围 ") as temporary:
        root = Path(temporary)
        config = load_config(workspace=root / "workspace")
        original = execute_manifest(write_manifest(root / "first"), config, allow_synthetic=True)

        def bad_dates(table, rows):
            for row in rows:
                row["NOTICE_DATE"] = None
                row["_revision_sequence"] = 2

        smaller = {**config, "_output_explicit": True, "output_periods": ("2024-03-31",)}
        bad = execute_manifest(
            write_manifest(root / "bad", bad_dates, "2025-05-02T08:00:00.000000Z"),
            smaller,
            allow_synthetic=True,
        )
        assert bad["status"] == "BLOCKED"
        assert bad["scope_id"] != original["scope_id"]
        with Store(Path(config["workspace"]), readonly=True) as store:
            assert store.scope()["scope_id"] == bad["scope_id"]
            with pytest.raises(ValueError, match="blocked/stale"):
                store.select_snapshot(scope_id=original["scope_id"])
            assert (
                store.select_snapshot(snapshot_id=original["snapshot_id"])["status"] == "PUBLISHED"
            )


def test_old_missing_capture_does_not_erase_a_report_found_by_another_scope():
    """库内重放旧空结果时，不能把另一个范围更晚取得的真实观察事实删回missing。"""
    with tempfile.TemporaryDirectory(prefix="finpipe 人工跨范围补报 ") as temporary:
        root = Path(temporary)
        config = load_config(workspace=root / "workspace")

        def missing_income(table, rows):
            if table == "income":
                rows[:] = [
                    row
                    for row in rows
                    if not (
                        row["SECUCODE"] == "600519.SH"
                        and row["REPORT_DATE"].startswith("2024-06-30")
                    )
                ]

        old = write_manifest(root / "missing", missing_income)
        first = execute_manifest(old, config, allow_synthetic=True)
        assert first["status"] == "PUBLISHED"
        smaller = {**config, "_output_explicit": True, "output_periods": ("2024-03-31",)}
        newer = execute_manifest(
            write_manifest(root / "found", observed_at="2025-05-02T08:00:00.000000Z"),
            smaller,
            allow_synthetic=True,
        )
        assert newer["status"] == "PUBLISHED"
        rebuilt = execute_manifest(old, config, allow_synthetic=True)
        assert rebuilt["status"] == "PUBLISHED"
        with Store(Path(config["workspace"]), readonly=True) as store:
            feature = store.features(
                rebuilt["snapshot_id"], "600519.SH", "2024-06-30", "net_margin_q"
            )[0]
            assert feature.feature_value is not None


def test_older_candidate_cannot_revive_values_after_newer_bad_input():
    """新批次已发现坏数据，即使head没变，也不能恢复旧好候选成为当前结果。"""
    with tempfile.TemporaryDirectory(prefix="finpipe 人工来源失效 ") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        config = load_config(workspace=workspace)
        first = execute_manifest(write_manifest(root / "first"), config, allow_synthetic=True)
        assert first["status"] == "PUBLISHED"

        def corrected(table, rows):
            if table == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["PARENT_NETPROFIT"] *= 1.1
                        row["_revision_sequence"] = 2

        path = write_manifest(root / "corrected", corrected, "2025-05-02T08:00:00.000000Z")
        with patch.dict("os.environ", {"FINPIPE_FAULT_AT": "after_prepare"}):
            candidate = execute_manifest(path, config, allow_synthetic=True)
        assert candidate["status"] == "FAILED"

        def bad_dates(table, rows):
            corrected(table, rows)
            for row in rows:
                row["NOTICE_DATE"] = None
                row["_revision_sequence"] = 3

        bad = execute_manifest(
            write_manifest(root / "bad", bad_dates, "2025-05-03T08:00:00.000000Z"),
            config,
            allow_synthetic=True,
        )
        assert bad["status"] == "BLOCKED"
        result = resume_run(workspace, candidate["run_id"])
        assert result["status"] == "SUPERSEDED"
        with Store(workspace, readonly=True) as store:
            assert store.head(first["scope_id"]) == first["snapshot_id"]
            assert store.scope(first["scope_id"])["health_status"] == "blocked"
