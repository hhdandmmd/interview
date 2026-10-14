"""共享工作区OS锁与显式故障检查点，避免各入口自行处理互斥。"""

import os
import sys
from pathlib import Path

from filelock import FileLock


def workspace_lock(workspace: Path) -> FileLock:
    """使用实际OS互斥；超时不抢占仍存活的锁拥有者。"""
    workspace.mkdir(parents=True, exist_ok=True)
    return FileLock(workspace / "workspace.lock", timeout=0)


def fault(point: str):
    """仅显式测试环境启用中断点，不把测试恢复作为生产完成证据。"""
    if os.environ.get("FINPIPE_FAULT_AT") == point:
        raise RuntimeError(f"受控故障点：{point}")
    if os.environ.get("FINPIPE_ABORT_AT") == point:
        os._exit(73)


def result_exit_code(result: dict) -> int:
    """所有入口遵守同一成功口径；已生成文件但范围受阻或过期仍返回非零。"""
    successful = result.get("status") in ("ok", "healthy", "PUBLISHED", "NO_CHANGE")
    backtest_ok = "backtest" not in result or result["backtest"].get("status") == "ok"
    return (
        0
        if successful and backtest_ok and result.get("scope_health") not in ("blocked", "stale")
        else 3
    )


def recovery_command(run_id: str, workspace: str | Path) -> str:
    """恢复使用实际程序路径；PowerShell单引号保留目录字面值，不执行其中的表达式。"""

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    prefix = "& " + quote(sys.executable)
    if not getattr(sys, "frozen", False):
        prefix += " -m finpipe"
    return f"{prefix} resume --run-id {quote(run_id)} --workspace {quote(workspace)}"
