"""共享工作区OS锁与显式故障检查点，避免各入口自行处理互斥。"""

import os
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
