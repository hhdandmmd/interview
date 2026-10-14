"""首次使用时的错误必须指出实际路径，且只读操作不能创建空工作区。"""

import json

import pytest

from finpipe.cli import main
from finpipe.storage import Store


@pytest.mark.parametrize("existing_directory", [False, True])
def test_missing_readonly_workspace_names_path_and_preserves_files(tmp_path, existing_directory):
    """路径写错与已有空目录都提供下一步，同时保持只读访问不写入文件。"""
    workspace = tmp_path / "尚未生成 财务数据"
    if existing_directory:
        workspace.mkdir()
    with pytest.raises(FileNotFoundError) as raised:
        Store(workspace, readonly=True)
    message = str(raised.value)
    assert str(workspace.resolve() / "catalog.sqlite") in message
    assert "--workspace" in message
    assert "demo、run 或 replay" in message
    assert workspace.exists() == existing_directory
    assert not (workspace / "catalog.sqlite").exists()


def test_query_wrong_workspace_returns_actionable_error(tmp_path, capsys):
    """CLI稳定返回输入错误JSON，而不是英文SQLite异常或未捕获traceback。"""
    workspace = tmp_path / "写错的 工作区"
    code = main(
        ["query", "--workspace", str(workspace), "--company", "600519.SH", "--period", "2025Q4"]
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == 2
    assert result["status"] == "FAILED"
    assert "未找到工作区数据库" in result["error"]
    assert str(workspace.resolve() / "catalog.sqlite") in result["error"]
    assert not captured.err
    assert not workspace.exists()


def test_corrupt_existing_database_keeps_actual_error(tmp_path):
    """已有文件损坏不能被误报为缺失，也不能被只读操作覆盖重建。"""
    import sqlite3

    workspace = tmp_path / "损坏的 财务数据"
    workspace.mkdir()
    database = workspace / "catalog.sqlite"
    original = b"intentionally invalid sqlite file"
    database.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        with Store(workspace, readonly=True) as store:
            store.conn.execute("SELECT * FROM sqlite_master").fetchall()
    assert database.read_bytes() == original
