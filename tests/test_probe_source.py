"""探测脚本的离线回归：模拟网络和等待，不修改真实来源证据。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_probe", ROOT / "scripts/probe_source.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
NOW = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
REPORT = "RPT_F10_FINANCE_GINCOME"
CODE = "600519.SH"
PERIOD = "2024-12-31"
PAYLOAD = {
    "success": True,
    "result": {
        "pages": 1,
        "count": 1,
        "data": [
            {
                "SECUCODE": CODE,
                "REPORT_DATE": PERIOD + " 00:00:00",
                "NOTICE_DATE": "2025-04-03 00:00:00",
                "OPERATE_INCOME": 100,
                "NETPROFIT": 10,
            }
        ],
    },
}


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.payload = copy.deepcopy(PAYLOAD)
        self.args = argparse.Namespace(period=PERIOD, timeout=8)
        self.sleep = self.enterContext(patch.object(probe.time, "sleep"))
        self.urlopen = self.enterContext(patch.object(probe.urllib.request, "urlopen"))
        self.enterContext(patch.object(probe, "datetime", FrozenDatetime))

    def fetch(self, responses=None):
        self.urlopen.side_effect = responses or [io.BytesIO(json.dumps(self.payload).encode())]
        return probe.fetch(REPORT, probe.REPORTS[REPORT], CODE, self.args)

    def test_valid_response_preserves_payload_and_observation(self):
        result = self.fetch()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attempts"][0]["payload"], self.payload)
        self.assertEqual(result["attempts"][0]["observed_at_utc"], NOW.isoformat())

    def test_nullable_amount_is_not_zero(self):
        self.payload["result"]["data"][0]["OPERATE_INCOME"] = None
        result = self.fetch()
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["attempts"][0]["payload"]["result"]["data"][0]["OPERATE_INCOME"])

    def test_zero_negative_and_numeric_string_are_accepted(self):
        for value in (0, -10, "1.25e2"):
            with self.subTest(value=value):
                self.payload["result"]["data"][0]["NETPROFIT"] = value
                self.assertEqual(self.fetch()["status"], "ok")

    def test_invalid_numeric_values_are_rejected(self):
        for value in (True, False, "corrupt", "3.2亿", float("inf"), float("nan")):
            with self.subTest(value=value):
                self.payload["result"]["data"][0]["OPERATE_INCOME"] = value
                result = self.fetch()
                self.assertEqual(result["status"], "schema_error")
                self.assertIn("OPERATE_INCOME", result["invalid_fields"])

    def test_missing_field_is_distinguished_from_null(self):
        del self.payload["result"]["data"][0]["NETPROFIT"]
        result = self.fetch()
        self.assertEqual(result["status"], "schema_error")
        self.assertEqual(result["missing_fields"], ["NETPROFIT"])

    def test_invalid_notice_dates_are_rejected(self):
        for value in (None, "2025-02-30", "2024-12-30", "2026-10-14", "20250403"):
            with self.subTest(value=value):
                self.payload["result"]["data"][0]["NOTICE_DATE"] = value
                result = self.fetch()
                self.assertEqual(result["status"], "schema_error")
                self.assertIn("NOTICE_DATE", result["invalid_fields"])

    def test_notice_day_uses_shanghai_not_host_or_utc(self):
        self.payload["result"]["data"][0]["NOTICE_DATE"] = "2026-10-13"
        self.assertEqual(self.fetch()["status"], "ok")

    def test_aware_source_timestamp_uses_shanghai_day(self):
        self.payload["result"]["data"][0]["NOTICE_DATE"] = "2026-10-12T15:00:00Z"
        self.assertEqual(self.fetch()["status"], "ok")

    def test_future_exact_notice_is_rejected_on_same_shanghai_day(self):
        for value in ("2026-10-12T17:00:00Z", "2026-10-13T01:00:00+08:00", "2026-10-13 01:00:00"):
            with self.subTest(value=value):
                self.payload["result"]["data"][0]["NOTICE_DATE"] = value
                result = self.fetch()
                self.assertEqual(result["status"], "schema_error")
                self.assertIn("NOTICE_DATE", result["invalid_fields"])

    def test_wrong_identity_or_period_is_rejected(self):
        for field, value in (("SECUCODE", "000333.SZ"), ("REPORT_DATE", "2024-09-30")):
            with self.subTest(field=field):
                self.payload = copy.deepcopy(PAYLOAD)
                self.payload["result"]["data"][0][field] = value
                self.assertEqual(self.fetch()["status"], "identity_error")

    def test_invalid_report_date_is_schema_error(self):
        self.payload["result"]["data"][0]["REPORT_DATE"] = "2024-12-31 broken"
        self.assertEqual(self.fetch()["status"], "schema_error")

    def test_hidden_extra_count_and_wrong_pages_are_rejected(self):
        for key, value in (("count", 2), ("pages", 2), ("count", True), ("pages", None)):
            with self.subTest(key=key, value=value):
                self.payload = copy.deepcopy(PAYLOAD)
                self.payload["result"][key] = value
                self.assertEqual(self.fetch()["status"], "schema_error")

    def test_extra_or_nondictionary_rows_are_rejected(self):
        for row in ("not a record", copy.deepcopy(PAYLOAD["result"]["data"][0])):
            with self.subTest(row=row):
                self.payload = copy.deepcopy(PAYLOAD)
                self.payload["result"]["data"].append(row)
                self.payload["result"]["count"] = 2
                self.assertEqual(self.fetch()["status"], "schema_error")

    def test_consistent_empty_response_is_empty(self):
        self.payload["result"] = {"pages": 0, "count": 0, "data": []}
        self.assertEqual(self.fetch()["status"], "empty")

    def test_empty_page_with_nonzero_count_is_schema_error(self):
        self.payload["result"]["data"] = []
        self.assertEqual(self.fetch()["status"], "schema_error")

    def test_business_error_does_not_retry(self):
        self.payload = {"success": False, "message": "bad parameter"}
        self.assertEqual(self.fetch()["status"], "api_error")
        self.assertEqual(self.urlopen.call_count, 1)

    def test_malformed_json_does_not_retry(self):
        self.assertEqual(self.fetch([io.BytesIO(b"not json")])["status"], "schema_error")
        self.assertEqual(self.urlopen.call_count, 1)

    def test_nonretryable_http_fails_once_with_status(self):
        error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, None)
        result = self.fetch([error])
        self.assertEqual(result["status"], "http_error")
        self.assertEqual(result["attempts"][0]["http_status"], 403)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_timeout_then_success_preserves_both_attempts(self):
        result = self.fetch(
            [
                urllib.error.URLError(TimeoutError("timeout")),
                io.BytesIO(json.dumps(PAYLOAD).encode()),
            ]
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])

    def test_transient_http_retries_are_bounded(self):
        errors = [
            urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {}, None)
            for _ in range(3)
        ]
        self.assertEqual(self.fetch(errors)["status"], "request_error")
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2, 4])

    def test_retry_after_delay_is_respected(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "Busy", {"Retry-After": "5"}, None
        )
        self.assertEqual(
            self.fetch([error, io.BytesIO(json.dumps(PAYLOAD).encode())])["status"], "ok"
        )
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 5])

    def test_long_retry_after_is_deferred_without_early_request(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "Busy", {"Retry-After": "120"}, None
        )
        result = self.fetch([error])
        self.assertEqual(result["status"], "retry_deferred")
        self.assertEqual(result["next_retry_at"], "2026-10-12T16:02:00+00:00")
        self.assertEqual(result["retry_after_seconds"], 120)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_http_date_retry_after_is_respected(self):
        header = format_datetime(datetime(2026, 10, 12, 16, 0, 6, tzinfo=timezone.utc), usegmt=True)
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "Busy", {"Retry-After": header}, None
        )
        self.assertEqual(
            self.fetch([error, io.BytesIO(json.dumps(PAYLOAD).encode())])["status"], "ok"
        )
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 6])

    def test_invalid_retry_after_uses_backoff(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "Busy", {"Retry-After": "not a date"}, None
        )
        self.assertEqual(
            self.fetch([error, io.BytesIO(json.dumps(PAYLOAD).encode())])["status"], "ok"
        )
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])

    def test_permanent_dns_or_certificate_error_does_not_retry(self):
        for reason in (
            socket.gaierror(socket.EAI_NONAME, "unknown host"),
            ssl.SSLCertVerificationError("bad certificate"),
        ):
            with self.subTest(reason=reason):
                self.urlopen.reset_mock()
                self.assertEqual(
                    self.fetch([urllib.error.URLError(reason)])["status"], "request_error"
                )
                self.assertEqual(self.urlopen.call_count, 1)

    def test_existing_real_evidence_still_validates_without_modification(self):
        path = ROOT / "data/evidence/source_probe.json"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        evidence = json.loads(path.read_bytes())
        for check in evidence["checks"]:
            attempt = next(a for a in reversed(check["attempts"]) if "payload" in a)
            period_key, fields = (probe.REPORTS | probe.SUMMARIES)[check["report"]]
            result = probe.validate_payload(
                attempt["payload"],
                period_key,
                fields,
                check["code"],
                evidence["period"],
                datetime.fromisoformat(attempt["observed_at_utc"]),
            )
            self.assertEqual(result["status"], "ok", check["report"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.urlopen.assert_not_called()

    def test_main_writes_only_requested_temporary_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            self.urlopen.side_effect = [io.BytesIO(json.dumps(PAYLOAD).encode())]
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "probe_source.py",
                        "--codes",
                        CODE,
                        "--report",
                        REPORT,
                        "--output",
                        str(output),
                    ],
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(probe.main(), 0)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["checks"][0]["status"], "ok")

    def test_main_deferred_retry_stops_other_requests_to_same_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "deferred.json"
            self.urlopen.side_effect = [
                urllib.error.HTTPError(
                    "https://example.invalid", 429, "Busy", {"Retry-After": "120"}, None
                )
            ]
            with (
                patch.object(
                    sys, "argv", ["probe_source.py", "--codes", CODE, "--output", str(output)]
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(probe.main(), 1)
            checks = json.loads(output.read_text(encoding="utf-8"))["checks"]
            self.assertEqual(len(checks), 3)
            self.assertEqual(checks[0]["status"], "retry_deferred")
            for check in checks[1:]:
                self.assertEqual(check["status"], "not_attempted")
                self.assertEqual(check["reason"], "retry_deferred")
                self.assertEqual(check["blocked_by"], {"code": CODE, "report": REPORT})
                self.assertEqual(check["next_retry_at"], "2026-10-12T16:02:00+00:00")
                self.assertEqual(check["attempts"], [])
            self.assertEqual(self.urlopen.call_count, 1)
            self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1])

    def test_main_rejects_invalid_arguments_before_request(self):
        for arguments in (["--period", "2024-12-30"], ["--codes", "333.SZ"], ["--timeout", "nan"]):
            with (
                self.subTest(arguments=arguments),
                patch.object(sys, "argv", ["probe_source.py", *arguments]),
                patch.object(sys, "stderr", io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    probe.main()
                self.assertEqual(error.exception.code, 2)
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
