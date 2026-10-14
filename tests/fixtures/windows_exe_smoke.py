"""在中文空格目录、仅系统PATH下检查EXE；不能据此宣称全新Windows验收。"""

import argparse
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path


def run_smoke(executable: Path, stage: str) -> dict:
    """只运行离线命令，临时工作区结束后清理，不覆盖真实证据。"""
    environment = os.environ.copy()
    system_path = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(system_path, len(system_path)):
        raise OSError("无法读取Windows系统目录")
    environment["PATH"] = system_path.value
    interpreters = [
        shutil.which(name, path=environment["PATH"]) for name in ("python", "python3", "py")
    ]
    if any(interpreters):
        raise AssertionError("系统PATH仍可找到Python或启动器，不能算移除PATH验证")
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["HTTP_PROXY"] = "http://127.0.0.1:9"
    environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
    with tempfile.TemporaryDirectory(prefix="finpipe EXE 中文验收 ") as temporary:
        directory = Path(temporary)
        workspace = directory / "财报 工作区"

        def invoke(*arguments):
            result = subprocess.run(
                [str(executable), *arguments, "--workspace", str(workspace)],
                cwd=directory,
                env=environment,
                capture_output=True,
                timeout=120,
            )
            stdout = result.stdout.decode("utf-8")
            stderr = result.stderr.decode("utf-8", errors="replace")
            if result.returncode:
                raise AssertionError(
                    f"EXE退出{result.returncode}：{stdout[:2000]}；{stderr[-2000:]}"
                )
            return json.loads(stdout)

        published = invoke("demo")
        queried = invoke(
            "query", "--company", "600519.SH", "--period", "2025Q4", "--feature", "roe_weighted_ytd"
        )
        healthy = invoke("health")
        if (
            published["status"] != "PUBLISHED"
            or len(queried["rows"]) != 1
            or healthy["status"] != "healthy"
        ):
            raise AssertionError("EXE离线行为不满足固定样例和健康契约")
        with closing(sqlite3.connect(workspace / "catalog.sqlite")) as connection:
            sources = connection.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0]
            features = connection.execute(
                "SELECT COUNT(*) FROM snapshot_features WHERE snapshot_id=?",
                (published["snapshot_id"],),
            ).fetchone()[0]
        if (sources, features) != (72, 180):
            raise AssertionError("EXE样例行数与冻结3公司×8季度不符")
        return {
            "stage": stage,
            "status": "passed",
            "source_versions": sources,
            "feature_cells": features,
            "chinese_space_cwd": True,
            "path_contains_python": any(interpreters),
            "network_condition": "离线入口及不可用HTTP代理；不是操作系统网络隔离",
            "clean_machine_verified": False,
            "executable": str(executable),
        }


if __name__ == "__main__":
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("executable", type=Path)
    arguments.add_argument("--stage", default="initial_build")
    values = arguments.parse_args()
    print(
        json.dumps(
            run_smoke(values.executable.resolve(), values.stage), ensure_ascii=False, indent=2
        )
    )
