"""独立交付复核：用Decimal和原输入检查分组价格收益、时间边界及源码/EXE一致性。"""

import argparse
import csv
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from finpipe.exports import sha256, write_json


def audit_report(directory: Path) -> dict:
    """复核所有成员而非只看漂亮的汇总；收益从独立CSV价格重算。"""
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    signal_payload = json.loads((directory / "signals.json").read_text(encoding="utf-8"))
    with (directory / "market/prices.csv").open(encoding="utf-8-sig", newline="") as stream:
        prices = {
            (row["security_code"], row["trade_date"]): Decimal(row["close"])
            for row in csv.DictReader(stream)
        }
    with (directory / "market/calendar.csv").open(encoding="utf-8-sig", newline="") as stream:
        calendar = [row["trade_date"] for row in csv.DictReader(stream)]
    signals = {row["feature_version_id"]: row for row in signal_payload["rows"]}
    individual, cumulative = {}, {}
    for member in report["memberships"]:
        signal = signals[member["feature_version_id"]]
        cutoff = datetime.fromisoformat(member["decision_date"] + "T15:00:00+08:00")
        available = datetime.fromisoformat(signal["available_at"].replace("Z", "+00:00"))
        assert available <= cutoff
        assert signal["feature_value"] == member["feature_value"]
        assert calendar.index(member["entry_date"]) == calendar.index(member["decision_date"]) + 1
        assert (
            calendar.index(member["exit_date"]) - calendar.index(member["entry_date"])
            == report["parameters"]["horizon"]
        )
        if member["gross_return"] is not None:
            entry = prices[member["security_code"], member["entry_date"]]
            exit_price = prices[member["security_code"], member["exit_date"]]
            expected = exit_price / entry - 1
            assert abs(expected - Decimal(str(member["gross_return"]))) < Decimal("1e-12")
            individual.setdefault((member["decision_date"], member["group"]), []).append(expected)
    cost = Decimal(str(report["parameters"]["cost_bps"])) / 10000
    for row in report["period_returns"]:
        if row["net_return"] is None:
            cumulative[row["group"]] = None
            continue
        values = individual[row["decision_date"], row["group"]]
        assert len(values) == row["members"]
        gross = sum(values) / len(values)
        net = (1 - cost) ** 2 * (1 + gross) - 1
        assert abs(net - Decimal(str(row["net_return"]))) < Decimal("1e-12")
        if cumulative.get(row["group"], Decimal(1)) is not None:
            cumulative[row["group"]] = cumulative.get(row["group"], Decimal(1)) * (1 + net)
    for row in report["summary"]["groups"]:
        if row["cumulative_return"] is not None:
            assert abs(
                cumulative[row["group"]] - 1 - Decimal(str(row["cumulative_return"]))
            ) < Decimal("1e-10")
    with (directory / "chart_data.csv").open(encoding="utf-8-sig", newline="") as stream:
        chart_rows = list(csv.DictReader(stream))
    chart_points = {(row["trade_date"], row["series"]): row["nav"] for row in chart_rows}
    assert len(chart_points) == len(chart_rows)
    expected_points = {}
    first = report["summary"]["first_entry_date"]
    if first is not None:
        expected_points.update(
            ((first, f"G{group}"), Decimal(1))
            for group in range(1, report["parameters"]["groups"] + 1)
        )
        expected_points[first, "同截面等权参考"] = Decimal(1)
    for row in report["nav"]:
        expected_points[row["exit_date"], f"G{row['group']}"] = (
            None if row["nav"] is None else Decimal(str(row["nav"]))
        )
    windows = {}
    for row in report["period_returns"]:
        windows.setdefault(row["exit_date"], []).append(row)
    reference = Decimal(1)
    for day, rows in sorted(windows.items()):
        if reference is None or any(row["net_return"] is None for row in rows):
            reference = None
        else:
            # 从独立个股收益汇总，组人数不同也不使用组收益的简单平均。
            values = [
                value for row in rows for value in individual[row["decision_date"], row["group"]]
            ]
            reference *= (1 - cost) ** 2 * (1 + sum(values) / len(values))
        expected_points[day, "同截面等权参考"] = reference
    assert chart_points.keys() == expected_points.keys()
    for key, expected in expected_points.items():
        if expected is None:
            assert not chart_points[key]
        else:
            assert abs(expected - Decimal(chart_points[key])) < Decimal("1e-10")
    hashes = json.loads((directory / "SHA256.json").read_text(encoding="utf-8"))
    for name, expected in hashes.items():
        assert sha256(directory / name) == expected
    svg = (directory / "nav.svg").read_text(encoding="utf-8")
    body = (directory / "report.html").read_text(encoding="utf-8")
    assert svg in body and "日期" in svg and "净值（初始值 = 1）" in svg
    return {
        "status": "passed",
        "report_sha256": sha256(directory / "report.json"),
        "members_verified": len(report["memberships"]),
        "group_windows_verified": len(report["period_returns"]),
        "chart_points_verified": len(chart_rows),
        "summary": report["summary"],
        "time_policy": report["signal_metadata"]["time_policy"],
    }


def verify_runtime(executable: Path, real_example: Path) -> dict:
    """中文路径、系统PATH和不可用代理下运行人工/真实复算，再与源码固定输入逐项对照。"""
    environment = os.environ.copy()
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
        raise OSError("无法取得系统目录")
    environment["PATH"] = buffer.value
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["HTTP_PROXY"] = environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
    assert all(
        shutil.which(name, path=buffer.value) is None for name in ("python", "python3", "py")
    )
    evidence = []
    with tempfile.TemporaryDirectory(prefix="finpipe 回测 中文 空格 ") as name:
        root = Path(name)
        programs = {
            "exe": [str(executable.resolve())],
            "source": [sys.executable, "-m", "finpipe"],
        }
        for kind in ("synthetic", "real_proxy"):
            outputs = {}
            for runtime, program in programs.items():
                target = root / f"{kind} {runtime}"
                options = (
                    ["--demo"]
                    if kind == "synthetic"
                    else [
                        "--signals",
                        str((real_example / "signals.json").resolve()),
                        "--market",
                        str((real_example / "market").resolve()),
                        "--time-policy",
                        "publication_proxy",
                        "--horizon",
                        "20",
                        "--start",
                        "2023-01-01",
                        "--end",
                        "2025-12-31",
                    ]
                )
                result = subprocess.run(
                    [*program, "backtest", *options, "--output", str(target)],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    timeout=90,
                )
                assert result.returncode == 0, result.stdout.decode("utf-8") + result.stderr.decode(
                    "utf-8", errors="replace"
                )
                assert not result.stderr
                audit = audit_report(target)
                outputs[runtime] = json.loads((target / "report.json").read_text(encoding="utf-8"))
                evidence.append(
                    {"kind": kind, "runtime": runtime, "returncode": result.returncode, **audit}
                )
            for name in ("nav.svg", "chart_data.csv", "report.html"):
                assert (root / f"{kind} exe" / name).read_bytes() == (
                    root / f"{kind} source" / name
                ).read_bytes(), name
            for field in (
                "parameters",
                "summary",
                "memberships",
                "period_returns",
                "nav",
                "exclusions",
            ):
                assert outputs["exe"][field] == outputs["source"][field], field
    return {
        "status": "passed",
        "checks": evidence,
        "system_path_without_python": True,
        "unavailable_proxy": True,
        "source_exe_equal": True,
        "clean_machine_verified": False,
    }


if __name__ == "__main__":
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--report", type=Path, required=True)
    arguments.add_argument("--executable", type=Path)
    arguments.add_argument("--output", type=Path, required=True)
    values = arguments.parse_args()
    if values.output.exists():
        raise ValueError("审核回执已存在，不覆盖")
    receipt = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report_audit": audit_report(values.report),
    }
    if values.executable:
        receipt["runtime_audit"] = verify_runtime(values.executable, values.report)
    write_json(values.output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
