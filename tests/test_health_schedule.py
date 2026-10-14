"""独立调度反例：人工模拟历史任务，离线重放不代表在线任务按期启动。"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixtures.financial_cases import write_manifest

from finpipe.config import load_config
from finpipe.identity import canonical_json
from finpipe.ingestion import load_manifest
from finpipe.ops import health
from finpipe.pipeline import _new_run, _register
from finpipe.storage import Store


def test_recent_offline_processing_does_not_reset_the_online_schedule():
    """已到期未启动的在线任务不能被刚完成的离线维护运行掩盖。"""
    with tempfile.TemporaryDirectory(prefix="finpipe 人工调度 ") as temporary:
        workspace = Path(temporary)
        path = write_manifest(workspace / "fixture")
        manifest = load_manifest(path)
        config = load_config(workspace=workspace)
        with Store(workspace) as store:
            scope_id, _, scope = _register(store, manifest, config)
            online_id = _new_run(store, scope_id, config, path)
            online = json.loads(
                store.conn.execute(
                    "SELECT plan_json FROM runs WHERE run_id=?", (online_id,)
                ).fetchone()[0]
            )
            online["collection"] = {"codes": scope["codes"], "periods": scope["input_periods"]}
            old_time = (
                (datetime.now(timezone.utc) - timedelta(days=2))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            with store.transaction():
                store.conn.execute(
                    "UPDATE runs SET started_at=?,heartbeat_at=?,status='SUCCEEDED',phase='DONE',plan_json=? WHERE run_id=?",
                    (old_time, old_time, canonical_json(online), online_id),
                )
            manual_id = _new_run(store, scope_id, config, path)
            with store.transaction():
                store.conn.execute(
                    "UPDATE runs SET status='SUCCEEDED',phase='DONE' WHERE run_id=?", (manual_id,)
                )
        diagnostic = health(workspace)
        assert "missed_start" in diagnostic["rules"]
