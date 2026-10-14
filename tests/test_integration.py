"""通过真实CLI、SQLite与固定导出验证财务主线，预期来自独立人工分数。"""

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as parquet
from filelock import Timeout
from fixtures.financial_cases import CAPTURED_AT, PERIODS, expected_rows, write_manifest

from finpipe.config import load_config
from finpipe.features import compute_features
from finpipe.ingestion.archive import ManifestError, load_manifest
from finpipe.models import BusinessKey, RecordState, SourceRecord
from finpipe.pipeline import execute_manifest, execute_online, resume_run, workspace_lock
from finpipe.transforms import normalize

PROJECT = Path(__file__).resolve().parents[1]


def feature_map(rows: list[dict]) -> dict:
    """按完整特征键比较值，避免导出行顺序影响结果。"""
    return {
        (row["security_code"], str(row["report_end"]), row["feature_name"]): row for row in rows
    }


class EvidenceIntegrationTests(unittest.TestCase):
    """验证夹具本身可按实际来源契约加载，原文篡改不会被缓存行掩盖。"""

    def test_fixture_loads_complete_scope(self):
        manifest = load_manifest(PROJECT / "tests/fixtures/synthetic_two_companies/manifest.json")
        self.assertEqual(manifest["capture_kind"], "synthetic")
        self.assertEqual(sum(len(page["rows"]) for page in manifest["pages"]), 48)
        self.assertEqual({page["observed_at"] for page in manifest["pages"]}, {CAPTURED_AT})

    def test_changed_body_rejected_even_with_cached_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(Path(directory))
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["pages"][0]["rows"] = []
            path.write_text(json.dumps(manifest), encoding="utf-8")
            body_path = path.parent / manifest["pages"][0]["body_path"]
            body_path.write_bytes(body_path.read_bytes() + b" ")
            with self.assertRaisesRegex(ManifestError, "SHA256"):
                load_manifest(path)


class PublicFinancialIntegrationTests(unittest.TestCase):
    """在文件来源、清洗与公开计算入口之间核验独立值，便于定位集成错误。"""

    def test_loaded_sources_match_all_independent_fractions(self):
        manifest = load_manifest(PROJECT / "tests/fixtures/synthetic_two_companies/manifest.json")
        profile = load_config()["profile"]
        records, states = [], []
        for page in manifest["pages"]:
            for index, row in enumerate(page["rows"]):
                source_id = f"synthetic-source-{page['report_type']}-{index}"
                key = BusinessKey(
                    "eastmoney", row["SECUCODE"], row["REPORT_DATE"][:10], page["report_type"]
                )
                source = SourceRecord(source_id, key, row, page["observed_at"])
                records.append(normalize(source, profile, "2025-05-03T08:00:00.000000Z"))
                states.append(
                    RecordState(
                        "state-" + source_id, key, "selected", source_id, page["observed_at"]
                    )
                )
        results = compute_features(
            records,
            states,
            tuple(manifest["codes"]),
            PERIODS[4:],
            "2025-05-03T08:00:00.000000Z",
            "synthetic-profile",
            "synthetic-independent-check",
            rules=profile["parameters"],
        )
        actual = feature_map([asdict(result) for result in results])
        expected = feature_map(expected_rows())
        self.assertEqual(set(actual), set(expected))
        for key, row in expected.items():
            with self.subTest(key=key, fraction=row["exact_fraction"]):
                self.assertAlmostEqual(
                    actual[key]["feature_value"], row["feature_value"], places=12
                )


class CLIIntegrationTests(unittest.TestCase):
    """每个场景使用独立中文临时目录，合成数据不能混入真实工作区。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="finpipe_人工验收_")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / "独立工作区"
        self.manifest = write_manifest(self.directory / "人工原文")
        self.config = self.directory / "测试参数.toml"
        self.config.write_text(
            "# 局部失效测试允许发布NULL，不能用于生产默认门禁。\n"
            "[quality]\nmin_usable_coverage = 0.0\nmax_invalid_rate = 1.0\n",
            encoding="utf-8",
        )

    def cli(
        self,
        *arguments: str,
        success: bool | None = True,
        fault_at: str | None = None,
        offline: bool = False,
        expected_exit: int | None = None,
    ) -> dict:
        """从项目外启动真实入口；失败也必须返回可理解的结构化错误。"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment.pop("FINPIPE_FAULT_AT", None)
        environment.pop("FINPIPE_ABORT_AT", None)
        if fault_at:
            environment["FINPIPE_FAULT_AT"] = fault_at
        entry = [str(PROJECT / "tests/fixtures/offline_entry.py")] if offline else ["-m", "finpipe"]
        result = subprocess.run(
            [sys.executable, *entry, *map(str, arguments), "--workspace", str(self.workspace)],
            cwd=self.directory,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        diagnostic = result.stdout[:1500] + result.stderr[-1500:]
        if success:
            self.assertEqual(result.returncode, 0, diagnostic)
        elif success is False:
            self.assertNotEqual(result.returncode, 0, diagnostic)
        if expected_exit is not None:
            self.assertEqual(result.returncode, expected_exit, diagnostic)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI未返回JSON：{result.stdout!r}；stderr={result.stderr!r}；{exc}")

    def replay(self, manifest: Path | None = None, relaxed: bool = False) -> dict:
        """默认执行正常门禁；边界夹具显式允许带问题结果。"""
        arguments = ["replay", "--manifest", str(manifest or self.manifest), "--allow-synthetic"]
        if relaxed:
            arguments.extend(("--config", str(self.config)))
        return self.cli(*arguments)

    def rows(self, snapshot_id: str | None = None) -> dict:
        arguments = ["query"]
        if snapshot_id:
            arguments.extend(("--snapshot", snapshot_id))
        return feature_map(self.cli(*arguments)["rows"])

    def demo_config(self) -> dict:
        """在线控制流采用真实离线样例范围，完整配置避免隐式修改默认参数。"""
        path = self.directory / "在线控制流测试.toml"
        path.write_text(
            '[scope]\ncodes = ["600519.SH", "000333.SZ", "000651.SZ"]\n'
            'input_start = "2024Q1"\ninput_end = "2025Q4"\n'
            'output_start = "2025Q1"\noutput_end = "2025Q4"\n',
            encoding="utf-8",
        )
        return load_config(path, self.workspace)

    def db_counts(self) -> tuple[int, int, int]:
        """重复重放不得虚构新的源观察、源内容或发布快照。"""
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "source_versions",
                    "source_observations",
                    "dataset_snapshots",
                )
            )

    def test_synthetic_requires_explicit_authorization(self):
        rejected = self.cli("replay", "--manifest", self.manifest, success=False)
        self.assertIn("--allow-synthetic", rejected["error"])
        self.assertFalse((self.workspace / "snapshots").exists())

    def test_synthetic_cannot_share_the_real_evidence_workspace(self):
        self.assertEqual(self.cli("demo")["status"], "PUBLISHED")
        counts = self.db_counts()
        rejected = self.cli(
            "replay", "--manifest", self.manifest, "--allow-synthetic", success=False
        )
        self.assertIn("不同工作区", rejected["error"])
        self.assertEqual(self.db_counts(), counts)

    def test_real_offline_demo_and_filtered_query_work_from_another_chinese_directory(self):
        published = self.cli("demo", offline=True)
        self.assertEqual(published["status"], "PUBLISHED")
        queried = self.cli(
            "query",
            "--company",
            "600519.SH",
            "--period",
            "2025Q4",
            "--feature",
            "roe_weighted_ytd",
            offline=True,
        )
        self.assertEqual(len(queried["rows"]), 1)
        self.assertEqual(queried["rows"][0]["security_code"], "600519.SH")
        self.assertEqual(queried["rows"][0]["report_end"], "2025-12-31")
        self.assertIsNotNone(queried["rows"][0]["feature_value"])
        self.assertEqual(self.cli("health", offline=True)["status"], "healthy")

    def test_online_run_is_committed_and_locked_before_collector_starts(self):
        config = self.demo_config()
        checked = []

        def archived_collector(codes, periods, capture, network, progress):
            # 只返回既有真实归档，观察时间不改；此测试不宣称执行了线上采集。
            with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
                run = connection.execute(
                    "SELECT status,phase,heartbeat_at,plan_json FROM runs"
                ).fetchone()
            self.assertEqual(run[:2], ("RUNNING", "COLLECTING"))
            plan = json.loads(run[3])
            self.assertEqual(tuple(plan["collection"]["periods"]), tuple(periods))
            self.assertEqual(set(codes), {"600519.SH", "000333.SZ", "000651.SZ"})
            self.assertEqual(len(periods), 8)
            contender = workspace_lock(self.workspace)
            with self.assertRaises(Timeout):
                contender.acquire(timeout=0)
            progress({"phase": "artificial_control_check"})
            with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
                heartbeat = connection.execute("SELECT heartbeat_at FROM runs").fetchone()[0]
            self.assertGreaterEqual(heartbeat, run[2])
            checked.append(True)
            shutil.copytree(PROJECT / "src/finpipe/resources/demo", capture)
            return capture / "manifest.json"

        with patch("finpipe.ingestion.collect", side_effect=archived_collector):
            result = execute_online(config, refresh="recent")
        self.assertEqual(checked, [True])
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(len(self.rows()), 180)

    def test_collection_resume_uses_a_new_session_and_the_frozen_scope(self):
        config = self.demo_config()
        sessions = []

        def interrupted_collector(codes, periods, capture, network, progress):
            sessions.append((tuple(codes), tuple(periods), capture))
            raise OSError("人工构造采集传输中断，未产生真实来源捕获")

        with patch("finpipe.ingestion.collect", side_effect=interrupted_collector):
            failed = execute_online(config)
        self.assertEqual(failed["status"], "FAILED")
        config["scope"]["codes"] = ["000651.SZ"]
        config["input_periods"] = ("2030-03-31",)

        def archived_collector(codes, periods, capture, network, progress):
            self.assertEqual((tuple(codes), tuple(periods)), sessions[0][:2])
            self.assertNotEqual(capture, sessions[0][2])
            with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
                run = connection.execute(
                    "SELECT status,phase,finished_at,plan_json FROM runs WHERE run_id=?",
                    (failed["run_id"],),
                ).fetchone()
            self.assertEqual(run[:3], ("RUNNING", "COLLECTING", None))
            plan = json.loads(run[3])
            self.assertEqual(Path(plan["manifest_path"]).parent, capture)
            sessions.append((tuple(codes), tuple(periods), capture))
            progress({"phase": "artificial_resume_check"})
            shutil.copytree(PROJECT / "src/finpipe/resources/demo", capture)
            return capture / "manifest.json"

        with patch("finpipe.ingestion.collect", side_effect=archived_collector):
            recovered = resume_run(self.workspace, failed["run_id"])
        self.assertEqual(recovered["status"], "PUBLISHED", recovered)
        self.assertEqual(recovered["run_id"], failed["run_id"])
        self.assertEqual(len(sessions), 2)
        # 返回旧真实归档只核验控制流；用固定快照审计，不宣称当前上游恢复。
        self.assertEqual(len(self.rows(recovered["snapshot_id"])), 180)

    def test_independent_report_reconciliation_exposes_uncovered_fields(self):
        published = self.cli("demo", offline=True)
        report_path = self.directory / "独立年报核对.json"
        result = self.cli(
            "reconcile",
            "--snapshot",
            published["snapshot_id"],
            "--reference",
            PROJECT / "data/reference/original_reports/reference_contract.json",
            "--output",
            report_path,
            offline=True,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["left_count"], 384)
        self.assertEqual(result["reference_count"], 34)
        self.assertEqual(result["counts"], {"matched": 33, "not_comparable": 1, "left_only": 350})
        self.assertAlmostEqual(result["comparison_coverage"], 33 / 34)
        self.assertAlmostEqual(result["reference_coverage"], 33 / 384)
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["snapshot_id"], published["snapshot_id"])
        self.assertEqual(len(saved["rows"]), 384)
        self.assertFalse(saved["historical_revision_basis_verified"])

    def test_full_hand_calculation_and_export_consistency(self):
        published = self.replay()
        self.assertEqual(published["status"], "PUBLISHED")
        self.assertIn("synthetic", published["scope_id"])
        actual = self.rows()
        expected = feature_map(expected_rows())
        self.assertEqual(set(actual), set(expected))
        for key, row in expected.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(
                    actual[key]["feature_value"], row["feature_value"], places=12
                )
                self.assertIn(actual[key]["feature_status"], ("ok", "warning"))
                self.assertTrue(actual[key]["data_available_at"])

        exported = Path(published["export_path"])
        arrow = parquet.read_table(exported / "features_long.parquet")
        parquet_rows = feature_map(arrow.to_pylist())
        self.assertEqual(set(parquet_rows), set(expected))
        self.assertEqual(arrow.num_rows, 120)
        with (exported / "features_long.csv").open(encoding="utf-8-sig", newline="") as stream:
            csv_rows = feature_map(list(csv.DictReader(stream)))
        for key, row in expected.items():
            self.assertAlmostEqual(
                parquet_rows[key]["feature_value"], row["feature_value"], places=12
            )
            self.assertAlmostEqual(
                float(csv_rows[key]["feature_value"]), row["feature_value"], places=12
            )
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            keys = connection.execute(
                "SELECT security_code,report_end,feature_name FROM snapshot_features WHERE snapshot_id=?",
                (published["snapshot_id"],),
            ).fetchall()
            self.assertEqual(set(keys), set(expected))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        receipt = json.loads((exported / "publication_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["snapshot_id"], published["snapshot_id"])
        self.assertTrue(receipt["published_at"])
        manifest = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["snapshot_id"], published["snapshot_id"])
        for entry in manifest["files"]:
            content = (exported / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
            self.assertEqual(len(content), entry["bytes"])

    def test_same_evidence_is_no_change_without_new_observations(self):
        first = self.replay()
        counts = self.db_counts()
        second = self.replay()
        self.assertEqual(second["status"], "NO_CHANGE")
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(self.db_counts(), counts)

    def test_latest_null_invalidates_dependencies_and_preserves_old_snapshot(self):
        first = self.replay(relaxed=True)
        previous = self.rows()

        def make_null(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["OPERATE_COST"] = None
                        row["_revision_sequence"] = 2

        changed = write_manifest(
            self.directory / "成本缺失", make_null, "2025-05-02T08:00:00.000000Z"
        )
        second = self.replay(changed, relaxed=True)
        self.assertEqual(second["status"], "PUBLISHED")
        current = self.rows()
        affected = {
            ("600519.SH", "2024-06-30", "gross_margin_q"),
            ("600519.SH", "2024-09-30", "gross_margin_q"),
            ("600519.SH", "2024-06-30", "gross_margin_ytd"),
        }
        for key in affected:
            self.assertIsNone(current[key]["feature_value"])
            self.assertTrue(current[key]["reason_codes"])
        for key in set(previous) - affected:
            self.assertAlmostEqual(
                current[key]["feature_value"], previous[key]["feature_value"], places=12
            )
        historic = self.rows(first["snapshot_id"])
        for key in previous:
            self.assertEqual(historic[key]["feature_value"], previous[key]["feature_value"])
        exported = Path(second["export_path"])
        arrow_rows = feature_map(parquet.read_table(exported / "features_long.parquet").to_pylist())
        for key in affected:
            self.assertIsNone(arrow_rows[key]["feature_value"])

    def test_missing_notice_cannot_revive_old_good_values(self):
        first = self.replay(relaxed=True)

        def make_bad_time(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["NOTICE_DATE"] = None
                        row["_revision_sequence"] = 2

        changed = write_manifest(
            self.directory / "公告缺失", make_bad_time, "2025-05-02T08:00:00.000000Z"
        )
        self.assertEqual(self.replay(changed, relaxed=True)["status"], "PUBLISHED")
        current = self.rows()
        for name in (
            "net_margin_q",
            "gross_margin_q",
            "revenue_growth_yoy",
            "parent_profit_growth_yoy",
        ):
            row = current[("600519.SH", "2024-06-30", name)]
            self.assertIsNone(row["feature_value"])
            self.assertTrue(row["data_available_at"])
        self.assertIsNotNone(
            self.rows(first["snapshot_id"])[("600519.SH", "2024-06-30", "net_margin_q")][
                "feature_value"
            ]
        )

    def test_conflicting_values_without_authoritative_order_are_not_guessed(self):
        self.replay(relaxed=True)

        def make_conflict(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["OPERATE_INCOME"] += 10
                        row["TOTAL_OPERATE_INCOME"] += 10
                        row["_evidence"]["authoritative_revision"] = False
                        row.pop("_revision_sequence")

        changed = write_manifest(
            self.directory / "无次序冲突", make_conflict, "2025-05-02T08:00:00.000000Z"
        )
        self.assertEqual(self.replay(changed, relaxed=True)["status"], "PUBLISHED")
        current = self.rows()
        row = current[("600519.SH", "2024-06-30", "net_margin_q")]
        self.assertIsNone(row["feature_value"])
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT state_status FROM record_states WHERE json_extract(business_key,'$.security_code')=? "
                    "AND json_extract(business_key,'$.report_end')=? AND json_extract(business_key,'$.statement_type')=? "
                    "ORDER BY state_sequence DESC LIMIT 1",
                    ("600519.SH", "2024-06-30", "income"),
                ).fetchone()[0],
                "conflict",
            )

    def test_sealed_candidate_is_invisible_then_resume_publishes_once(self):
        failed = self.cli(
            "replay",
            "--manifest",
            self.manifest,
            "--allow-synthetic",
            success=False,
            fault_at="after_seal",
        )
        self.assertEqual(failed["status"], "FAILED")
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM workspace_head").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM dataset_snapshots WHERE run_id=?",
                    (failed["run_id"],),
                ).fetchone()[0],
                "PREPARED",
            )
        self.cli("query", success=False)
        recovered = self.cli("resume", "--run-id", failed["run_id"])
        self.assertEqual(recovered["status"], "PUBLISHED")
        self.assertEqual(len(self.rows()), 120)
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM dataset_snapshots WHERE status='PUBLISHED'",
                ).fetchone()[0],
                1,
            )

    def test_hard_process_abort_after_rename_releases_lock_and_resumes_same_candidate(self):
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(PROJECT / "src"),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "FINPIPE_ABORT_AT": "after_rename",
            }
        )
        environment.pop("FINPIPE_FAULT_AT", None)
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "finpipe",
                "replay",
                "--manifest",
                str(self.manifest),
                "--allow-synthetic",
                "--workspace",
                str(self.workspace),
            ],
            cwd=self.directory,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(process.returncode, 73, process.stdout[:1500] + process.stderr[-1500:])
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            run_id, snapshot_id = connection.execute(
                "SELECT run_id,snapshot_id FROM runs"
            ).fetchone()
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM dataset_snapshots WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()[0],
                "PREPARED",
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM workspace_head").fetchone()[0], 0
            )
        self.assertTrue((self.workspace / "snapshots" / snapshot_id / "manifest.json").is_file())
        with workspace_lock(self.workspace):
            pass
        self.cli("query", "--snapshot", snapshot_id, success=False)
        recovered = self.cli("resume", "--run-id", run_id)
        self.assertEqual(recovered["status"], "PUBLISHED")
        self.assertEqual(recovered["snapshot_id"], snapshot_id)
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM dataset_snapshots WHERE status='PUBLISHED'",
                ).fetchone()[0],
                1,
            )
        receipt = json.loads(
            (Path(recovered["export_path"]) / "publication_receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["snapshot_id"], snapshot_id)
        self.assertEqual(receipt["publication_sequence"], 1)
        self.assertTrue(receipt["published_at"])
        self.assertEqual(len(self.rows()), 120)

    def test_old_candidate_resume_never_regresses_new_head(self):
        first = self.replay()

        def revision(sequence, revenue):
            def mutate(report_type, rows):
                if report_type == "income":
                    for row in rows:
                        if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                            "2024-06-30"
                        ):
                            row["OPERATE_INCOME"] = revenue
                            row["TOTAL_OPERATE_INCOME"] = revenue
                            row["_revision_sequence"] = sequence

            return mutate

        old_manifest = write_manifest(
            self.directory / "旧候选", revision(2, 360), "2025-05-02T08:00:00.000000Z"
        )
        failed = self.cli(
            "replay",
            "--manifest",
            old_manifest,
            "--allow-synthetic",
            success=False,
            fault_at="after_prepare",
        )
        self.assertEqual(failed["status"], "FAILED")
        newer_manifest = write_manifest(
            self.directory / "新修订", revision(3, 380), "2025-05-03T08:00:00.000000Z"
        )
        latest = self.replay(newer_manifest)
        self.assertEqual(latest["status"], "PUBLISHED")
        before_resume = self.rows()
        result = self.cli("resume", "--run-id", failed["run_id"], success=None)
        self.assertEqual(result["status"], "SUPERSEDED")
        self.assertEqual(self.rows(), before_resume)
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT snapshot_id FROM workspace_head WHERE scope_id=?",
                    (latest["scope_id"],),
                ).fetchone()[0],
                latest["snapshot_id"],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM dataset_snapshots WHERE run_id=?",
                    (failed["run_id"],),
                ).fetchone()[0],
                "SUPERSEDED",
            )
        self.assertEqual(len(self.rows(first["snapshot_id"])), 120)

    def test_failed_run_without_candidate_cannot_mutate_a_newer_published_head(self):
        self.replay()
        failed_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        failed_manifest = write_manifest(self.directory / "未生成候选的失败", observed_at=failed_at)
        manifest = json.loads(failed_manifest.read_text(encoding="utf-8"))
        manifest["complete"] = False
        manifest["errors"] = [{"error": "人工构造未完成扫描", "attempts": []}]
        failed_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        failed = self.cli(
            "replay", "--manifest", failed_manifest, "--allow-synthetic", success=False
        )

        def corrected_revenue(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["OPERATE_INCOME"] = 380
                        row["TOTAL_OPERATE_INCOME"] = 380
                        row["_revision_sequence"] = 3

        current_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        corrected = write_manifest(self.directory / "失败后新修订", corrected_revenue, current_at)
        latest = self.replay(corrected)
        before = self.rows()
        counts = self.db_counts()
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT snapshot_id FROM runs WHERE run_id=?",
                    (failed["run_id"],),
                ).fetchone()[0]
            )
            states = connection.execute("SELECT COUNT(*) FROM record_states").fetchone()[0]
        resumed = self.cli("resume", "--run-id", failed["run_id"], success=None)
        self.assertEqual(resumed["status"], "SUPERSEDED")
        self.assertEqual(self.db_counts(), counts)
        self.assertEqual(self.rows(), before)
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM record_states").fetchone()[0], states
            )
            self.assertEqual(
                connection.execute(
                    "SELECT snapshot_id FROM workspace_head WHERE scope_id=?",
                    (latest["scope_id"],),
                ).fetchone()[0],
                latest["snapshot_id"],
            )

    def test_resume_cannot_publish_an_incomplete_or_wrong_scope_seal(self):
        failed = self.cli(
            "replay",
            "--manifest",
            self.manifest,
            "--allow-synthetic",
            success=False,
            fault_at="after_seal",
        )
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            snapshot_id, scope_id = connection.execute(
                "SELECT snapshot_id,scope_id FROM dataset_snapshots WHERE run_id=?",
                (failed["run_id"],),
            ).fetchone()
        path = self.workspace / "staging" / snapshot_id / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["scope_id"] = "synthetic-wrong-scope"
        manifest["files"] = [
            entry for entry in manifest["files"] if entry["path"] != "features_long.parquet"
        ]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.cli("resume", "--run-id", failed["run_id"], success=None)
        if result["status"] == "PUBLISHED":
            # 从DB重建并重新封存允许成功；原错误封存不能直接激活。
            sealed = json.loads(
                (Path(result["export_path"]) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sealed["scope_id"], scope_id)
            self.assertIn("features_long.parquet", {entry["path"] for entry in sealed["files"]})
        else:
            with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM workspace_head").fetchone()[0], 0
                )

    def test_committed_publication_resume_repairs_receipt_without_republishing(self):
        failed = self.cli(
            "replay",
            "--manifest",
            self.manifest,
            "--allow-synthetic",
            success=False,
            fault_at="after_activate",
        )
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            before = connection.execute(
                "SELECT snapshot_id,published_at,publication_sequence FROM dataset_snapshots WHERE run_id=?",
                (failed["run_id"],),
            ).fetchone()
            self.assertTrue(before[1])
        recovered = self.cli("resume", "--run-id", failed["run_id"])
        self.assertEqual(recovered["status"], "PUBLISHED")
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            after = connection.execute(
                "SELECT snapshot_id,published_at,publication_sequence FROM dataset_snapshots WHERE run_id=?",
                (failed["run_id"],),
            ).fetchone()
        self.assertEqual(after, before)
        receipt = json.loads(
            (Path(recovered["export_path"]) / "publication_receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["published_at"], before[1])
        self.assertEqual(len(self.rows()), 120)

    def test_backup_remains_queryable_and_replayable_without_original_directory(self):
        first = self.replay()
        expected = self.rows()
        backup = self.cli("backup", "--output", self.directory / "备份集合")
        self.assertEqual(backup["status"], "ok")
        original = self.workspace.resolve()
        hidden = self.directory / "原目录已移走"
        self.assertTrue(original.is_relative_to(self.directory.resolve()))
        self.assertTrue(hidden.resolve().is_relative_to(self.directory.resolve()))
        original.rename(hidden)
        self.workspace = Path(backup["backup_path"])
        self.assertEqual(self.rows(first["snapshot_id"]), expected)
        self.assertEqual(self.cli("doctor")["status"], "ok")
        replayed = self.cli("replay", "--from-db", "--allow-synthetic")
        self.assertEqual(replayed["status"], "NO_CHANGE")
        self.assertEqual(replayed["snapshot_id"], first["snapshot_id"])

    def test_health_reads_while_business_writer_lock_is_held(self):
        self.replay()
        with workspace_lock(self.workspace):
            result = self.cli("health")
        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["diagnostic_complete"])
        self.assertFalse(result["persisted"])

    def test_health_database_busy_records_unknown_fallback(self):
        self.replay()
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            try:
                result = self.cli("health", success=None)
            finally:
                connection.rollback()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["diagnostic_complete"])
        self.assertFalse(result["persisted"])
        saved = json.loads(
            (self.workspace / "diagnostics" / (result["event_id"] + ".json")).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(saved["event_id"], result["event_id"])

    def test_alert_acknowledgment_does_not_resolve_the_quality_failure(self):
        first = self.replay()

        def make_invalid(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        for field in (
                            "OPERATE_INCOME",
                            "TOTAL_OPERATE_INCOME",
                            "OPERATE_COST",
                            "NETPROFIT",
                            "PARENT_NETPROFIT",
                        ):
                            row[field] = True
                        row["_revision_sequence"] = 2

        changed = write_manifest(
            self.directory / "非法金额", make_invalid, "2025-05-02T08:00:00.000000Z"
        )
        blocked = self.cli("replay", "--manifest", changed, "--allow-synthetic", success=False)
        self.assertEqual(blocked["status"], "BLOCKED")
        alerts = self.cli("alerts")["rows"]
        failing = [
            row for row in alerts if json.loads(row["payload_json"])["run_id"] == blocked["run_id"]
        ]
        self.assertTrue(failing)
        alert_id = failing[0]["alert_id"]
        acknowledged = self.cli("alerts", "--ack", alert_id)["rows"]
        alert = next(row for row in acknowledged if row["alert_id"] == alert_id)
        self.assertEqual(alert["status"], "acknowledged")
        self.assertIsNone(alert["resolved_at"])
        self.assertIsNone(alert["recovery_run_id"])
        self.cli("query", success=False)
        stale = self.cli("query", "--allow-stale")
        self.assertEqual(stale["snapshot_id"], first["snapshot_id"])
        self.assertEqual(stale["health"], "blocked")

        def valid_revision(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["_revision_sequence"] = 3

        repaired = write_manifest(
            self.directory / "实际修复", valid_revision, "2025-05-03T08:00:00.000000Z"
        )
        recovery = self.replay(repaired)
        self.assertEqual(recovery["status"], "PUBLISHED")
        resolved = next(row for row in self.cli("alerts")["rows"] if row["alert_id"] == alert_id)
        self.assertEqual(resolved["status"], "resolved")
        self.assertTrue(resolved["resolved_at"])
        self.assertEqual(resolved["recovery_run_id"], recovery["run_id"])

        def recurring_invalid(report_type, rows):
            make_invalid(report_type, rows)
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["_revision_sequence"] = 4

        recurring = write_manifest(
            self.directory / "异常再次发生", recurring_invalid, "2025-05-04T08:00:00.000000Z"
        )
        new_failure = self.cli(
            "replay", "--manifest", recurring, "--allow-synthetic", success=False
        )
        self.assertEqual(new_failure["status"], "BLOCKED")
        reopened = [
            row
            for row in self.cli("alerts")["rows"]
            if json.loads(row["payload_json"])["run_id"] == new_failure["run_id"]
        ]
        self.assertTrue(reopened)
        self.assertTrue(all(row["status"] == "open" for row in reopened))
        self.assertTrue(
            all(row["resolved_at"] is None and row["recovery_run_id"] is None for row in reopened)
        )

    def test_doctor_detects_manifest_hash_change_even_when_json_is_valid(self):
        first = self.replay()
        manifest = Path(first["export_path"]) / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" \n")
        result = self.cli("doctor", success=False)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["failures"])

    def test_doctor_rejects_changed_raw_evidence_even_when_published_files_are_intact(self):
        self.replay()
        capture_path = next((self.workspace / "raw").glob("*/manifest.json"))
        manifest = json.loads(capture_path.read_text(encoding="utf-8"))
        body = capture_path.parent / manifest["pages"][0]["body_path"]
        body.write_bytes(body.read_bytes() + b" \n")
        result = self.cli("doctor", success=False)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["failures"])

    def test_health_alert_requires_a_later_complete_healthy_probe_to_resolve(self):
        self.replay()
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            try:
                failed_probe = self.cli("health", success=None)
            finally:
                connection.rollback()
        self.assertEqual(failed_probe["status"], "unknown")
        self.cli("doctor", "--repair")
        alert = next(
            row for row in self.cli("alerts")["rows"] if row["event_id"] == failed_probe["event_id"]
        )
        self.assertEqual(alert["status"], "open")
        self.assertIsNone(alert["resolved_at"])
        database_hash = hashlib.sha256((self.workspace / "catalog.sqlite").read_bytes()).hexdigest()
        recovered = self.cli("health")
        self.assertEqual(recovered["status"], "healthy")
        self.assertTrue(recovered["diagnostic_complete"])
        self.assertFalse(recovered["persisted"])
        self.assertEqual(
            hashlib.sha256((self.workspace / "catalog.sqlite").read_bytes()).hexdigest(),
            database_hash,
        )
        # 独立探测只写诊断文件，由持写锁的doctor补入新的真实恢复证据。
        self.cli("doctor", "--repair")
        resolved = next(
            row for row in self.cli("alerts")["rows"] if row["event_id"] == failed_probe["event_id"]
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertTrue(resolved["resolved_at"])

    def test_explain_uses_exact_fields_and_as_of_uses_actual_publication_time(self):
        published = self.replay()
        explained = self.cli(
            "explain",
            "--company",
            "600519.SH",
            "--period",
            "2024Q2",
            "--feature",
            "gross_margin_q",
        )
        self.assertEqual(explained["snapshot_id"], published["snapshot_id"])
        self.assertEqual(len(explained["rows"]), 1)
        inputs = explained["rows"][0]["inputs"]
        actual = {
            (row["key"]["report_end"], row["field_name"]): row["input_value"] for row in inputs
        }
        self.assertEqual(
            actual,
            {
                ("2024-06-30", "operating_revenue_ytd"): 348.0,
                ("2024-06-30", "operating_cost_ytd"): 186.0,
                ("2024-03-31", "operating_revenue_ytd"): 150.0,
                ("2024-03-31", "operating_cost_ytd"): 84.0,
            },
        )
        self.assertTrue(all(row["key"]["security_code"] == "600519.SH" for row in inputs))
        self.assertTrue(all(row["record_state_id"] and row["clean_version_id"] for row in inputs))
        # 人工来源观察在2025年，项目实际发布在当前运行时；前者不能伪造过去发布。
        self.cli("query", "--as-of", "2025-05-03", success=False)

    def test_explicit_snapshot_and_scope_must_agree(self):
        published = self.replay()
        rejected = self.cli(
            "query",
            "--snapshot",
            published["snapshot_id"],
            "--scope",
            "synthetic_scope_missing",
            success=False,
        )
        self.assertEqual(rejected["status"], "FAILED")

    def test_new_observation_of_original_content_restores_a_after_bad_metadata_b(self):
        def metadata_mode(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["_evidence"]["authoritative_revision"] = False
                        row.pop("_revision_sequence")

        original_manifest = write_manifest(self.directory / "元数据A", metadata_mode)
        first = self.replay(original_manifest, relaxed=True)
        original = self.rows()

        def bad_metadata(report_type, rows):
            metadata_mode(report_type, rows)
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["NOTICE_DATE"] = None

        bad = write_manifest(
            self.directory / "元数据B", bad_metadata, "2025-05-02T08:00:00.000000Z"
        )
        self.assertEqual(self.replay(bad, relaxed=True)["status"], "PUBLISHED")
        self.assertIsNone(self.rows()[("600519.SH", "2024-06-30", "net_margin_q")]["feature_value"])
        sources_after_b = self.db_counts()[0]
        restored = write_manifest(
            self.directory / "再次捕获A", metadata_mode, "2025-05-03T08:00:00.000000Z"
        )
        recovered = self.replay(restored, relaxed=True)
        self.assertEqual(recovered["status"], "PUBLISHED")
        self.assertEqual(self.db_counts()[0], sources_after_b)
        current = self.rows()
        for key, row in original.items():
            self.assertEqual(current[key]["feature_value"], row["feature_value"])
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            history = connection.execute(
                "SELECT selected_source_version_id FROM record_states WHERE json_extract(business_key,'$.security_code')=? "
                "AND json_extract(business_key,'$.report_end')=? AND json_extract(business_key,'$.statement_type')=? "
                "ORDER BY state_sequence",
                ("600519.SH", "2024-06-30", "income"),
            ).fetchall()
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0], history[2])
        self.assertNotEqual(history[0], history[1])
        self.assertEqual(len(self.rows(first["snapshot_id"])), 120)

    def test_incomplete_capture_cannot_be_no_change_or_claim_a_complete_scan(self):
        first = self.replay()
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            states_before = connection.execute("SELECT COUNT(*) FROM record_states").fetchone()[0]
        failed_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        path = write_manifest(self.directory / "上游捕获失败", observed_at=failed_at)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["complete"] = False
        manifest["errors"] = [
            {
                "report_type": "balance",
                "error": "人工构造的上游请求失败",
                "attempts": [],
                "observed_at": failed_at,
            }
        ]
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        failed = self.cli("replay", "--manifest", path, "--allow-synthetic", success=False)
        self.assertIn(failed["status"], ("BLOCKED", "FAILED"))
        self.assertTrue(failed["run_id"])
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM record_states").fetchone()[0],
                states_before,
            )
        self.cli("query", success=False)
        self.assertEqual(len(self.rows(first["snapshot_id"])), 120)

        # 旧归档重放不证明上游刚刚恢复，数据值一样也不能自动消除健康阻断。
        old_replay = self.cli(
            "replay",
            "--manifest",
            self.manifest,
            "--allow-synthetic",
            success=False,
            expected_exit=3,
        )
        self.assertEqual(old_replay["status"], "NO_CHANGE")
        self.assertEqual(old_replay["scope_health"], "blocked")
        self.cli("query", success=False)
        fresh_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        fresh = write_manifest(self.directory / "本轮完整新扫描", observed_at=fresh_at)
        recovery = self.replay(fresh)
        self.assertEqual(recovery["status"], "NO_CHANGE")
        self.assertEqual(recovery["snapshot_id"], first["snapshot_id"])
        self.assertEqual(len(self.rows()), 120)

    def test_recent_partial_scan_preserves_unscanned_members_and_full_output_scope(self):
        first = self.replay(relaxed=True)
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            scope = json.loads(
                connection.execute(
                    "SELECT content_json FROM scopes WHERE scope_id=?",
                    (first["scope_id"],),
                ).fetchone()[0]
            )
            earlier = connection.execute(
                "SELECT business_key,record_state_id,selected_clean_version_id,data_available_at,source_available_at "
                "FROM snapshot_records WHERE snapshot_id=? AND report_end<'2024-01-01' ORDER BY business_key",
                (first["snapshot_id"],),
            ).fetchall()

        def recent_null(report_type, rows):
            if report_type == "income":
                for row in rows:
                    if row["SECUCODE"] == "600519.SH" and row["REPORT_DATE"].startswith(
                        "2024-06-30"
                    ):
                        row["OPERATE_COST"] = None
                        row["_revision_sequence"] = 2

        partial = write_manifest(
            self.directory / "最近四季度",
            recent_null,
            "2025-05-02T08:00:00.000000Z",
            periods=PERIODS[4:],
        )
        self.assertEqual(sum(len(page["rows"]) for page in load_manifest(partial)["pages"]), 24)
        result = execute_manifest(
            partial,
            load_config(self.config, self.workspace),
            allow_synthetic=True,
            frozen_scope=scope,
        )
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["scope_id"], first["scope_id"])
        self.assertEqual(result["counts"]["evaluated"], 60)
        self.assertEqual(result["counts"]["reused"], 60)
        current = self.rows()
        self.assertEqual(len(current), 120)
        self.assertIsNone(current[("600519.SH", "2024-06-30", "gross_margin_q")]["feature_value"])
        with closing(sqlite3.connect(self.workspace / "catalog.sqlite")) as connection:
            later = connection.execute(
                "SELECT business_key,record_state_id,selected_clean_version_id,data_available_at,source_available_at "
                "FROM snapshot_records WHERE snapshot_id=? AND report_end<'2024-01-01' ORDER BY business_key",
                (result["snapshot_id"],),
            ).fetchall()
        self.assertEqual(later, earlier)
        replayed = self.cli("replay", "--from-db", "--allow-synthetic", "--config", self.config)
        self.assertEqual(replayed["status"], "NO_CHANGE")
        self.assertEqual(replayed["scope_id"], first["scope_id"])
        self.assertEqual(replayed["snapshot_id"], result["snapshot_id"])
        self.assertEqual(self.rows(), current)


if __name__ == "__main__":
    unittest.main()
