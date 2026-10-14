"""管理短事务、不可变版本和按范围的条件发布；网络不在事务内运行。"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from finpipe.catalog import FIELDS
from finpipe.identity import canonical_json
from finpipe.models import (
    BusinessKey,
    CleanRecord,
    FeatureInput,
    FeatureResult,
    RecordState,
    SourceRecord,
)
from finpipe.timeutil import utc_now

IMMUTABLE = (
    "processing_profiles",
    "universe_snapshots",
    "source_versions",
    "source_observations",
    "clean_versions",
    "record_states",
    "feature_versions",
    "feature_inputs",
    "snapshot_records",
    "snapshot_features",
    "http_attempts",
    "run_inputs",
)


def key_text(key: BusinessKey) -> str:
    """序列化完整业务键，不把合并口径与scope身份混淆。"""
    return canonical_json(asdict(key))


def decode_clean(payload: str) -> CleanRecord:
    """回读精确清洗结果，不重新解释旧快照时间。"""
    data = json.loads(payload)
    data["key"] = BusinessKey(**data["key"])
    data["reasons"] = {k: tuple(v) for k, v in data["reasons"].items()}
    data["evidence_flags"] = tuple(data["evidence_flags"])
    return CleanRecord(**data)


def decode_state(payload: str) -> RecordState:
    """恢复不可变裁决事实。"""
    data = json.loads(payload)
    data["key"] = BusinessKey(**data["key"])
    return RecordState(**data)


def decode_feature(payload: str) -> FeatureResult:
    """恢复因子及准确输入，包含未满足依赖。"""
    data = json.loads(payload)
    inputs = []
    for value in data["inputs"]:
        value["key"] = BusinessKey(**value["key"])
        value["reason_codes"] = tuple(value["reason_codes"])
        inputs.append(FeatureInput(**value))
    data["inputs"] = tuple(inputs)
    data["reason_codes"] = tuple(data["reason_codes"])
    data["evidence_flags"] = tuple(data["evidence_flags"])
    return FeatureResult(**data)


class Store:
    """单工作区的SQLite访问；调用方持OS写锁，health使用只读连接。"""

    def __init__(self, workspace: Path, readonly: bool = False):
        self.workspace = workspace.resolve()
        path = self.workspace / "catalog.sqlite"
        if readonly:
            if not path.is_file():
                raise FileNotFoundError(
                    f"未找到工作区数据库：{path}。请核对 --workspace 路径；"
                    "新工作区须先执行 demo、run 或 replay，再查询已有结果。"
                )
            self.conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        else:
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.conn.execute("PRAGMA journal_mode=DELETE")
            self.conn.execute("PRAGMA synchronous=FULL")
            self._migrate()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()

    def _migrate(self):
        """首次建立编号迁移；未知结构拒绝打开，不能无痕重建用户库。"""
        tables = {
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables:
            migration = Path(__file__).parents[1] / "resources/migrations/001_initial.sql"
            self.conn.executescript("BEGIN IMMEDIATE;\n" + migration.read_text(encoding="utf-8"))
            for name, specs in FIELDS.items():
                columns = ", ".join(f'"{spec.name}" REAL' for spec in specs)
                self.conn.execute(
                    f"CREATE TABLE clean_{name}(clean_version_id TEXT PRIMARY KEY REFERENCES clean_versions,{columns})"
                )
            for table in IMMUTABLE + tuple("clean_" + name for name in FIELDS):
                for action in ("UPDATE", "DELETE"):
                    self.conn.execute(
                        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'不可变业务版本禁止修改'); END"
                    )
            self.conn.commit()
        elif (
            "schema_version" not in tables
            or self.conn.execute("SELECT version FROM schema_version").fetchone()[0] != 1
        ):
            raise ValueError("数据库结构版本未知，须备份后执行明确迁移")

    @contextmanager
    def transaction(self):
        """把比较和更新包含在一个写事务；异常完整回滚。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def profile(self, profile_id: str) -> dict:
        row = self.conn.execute(
            "SELECT content_json FROM processing_profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if not row:
            raise ValueError("处理profile不存在")
        return json.loads(row[0])

    def head(self, scope_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT snapshot_id FROM workspace_head WHERE scope_id=?", (scope_id,)
        ).fetchone()
        return row[0] if row else None

    def scope(self, scope_id: str | None = None) -> sqlite3.Row:
        if scope_id:
            row = self.conn.execute("SELECT * FROM scopes WHERE scope_id=?", (scope_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT s.* FROM scopes s LEFT JOIN runs r USING(scope_id) "
                "ORDER BY COALESCE(r.finished_at,r.heartbeat_at,r.started_at,s.last_success_at,'') DESC,s.rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise ValueError("工作区没有该数据范围")
        return row

    def latest_state(self, key: BusinessKey) -> RecordState | None:
        row = self.conn.execute(
            "SELECT payload_json FROM record_states WHERE business_key=? ORDER BY state_sequence DESC LIMIT 1",
            (key_text(key),),
        ).fetchone()
        return decode_state(row[0]) if row else None

    def source(self, source_id: str) -> SourceRecord:
        row = self.conn.execute(
            "SELECT * FROM source_versions WHERE source_version_id=?", (source_id,)
        ).fetchone()
        if not row:
            raise ValueError("来源版本不存在")
        return SourceRecord(
            source_id,
            BusinessKey(**json.loads(row["business_key"])),
            json.loads(row["raw_json"]),
            row["first_seen_at"],
        )

    def save_source(self, record: SourceRecord) -> bool:
        key = record.key
        return bool(
            self.conn.execute(
                "INSERT OR IGNORE INTO source_versions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record.source_version_id,
                    key_text(key),
                    key.source,
                    key.security_code,
                    key.report_end,
                    key.statement_type,
                    key.consolidation_scope,
                    record.first_seen_at,
                    canonical_json(record.raw),
                ),
            ).rowcount
        )

    def save_state(self, state: RecordState):
        sequence = self.conn.execute(
            "SELECT COALESCE(MAX(state_sequence),0)+1 FROM record_states WHERE business_key=?",
            (key_text(state.key),),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT OR IGNORE INTO record_states VALUES(?,?,?,?,?,?,?)",
            (
                state.record_state_id,
                key_text(state.key),
                sequence,
                state.state_status,
                state.selected_source_version_id,
                state.decision_observed_at,
                canonical_json(state),
            ),
        )

    def save_clean(self, record: CleanRecord, profile: dict) -> bool:
        added = self.conn.execute(
            "INSERT OR IGNORE INTO clean_versions VALUES(?,?,?,?,?,?)",
            (
                record.clean_version_id,
                record.source_version_id,
                profile["mapping_version"],
                profile["cleaning_rule_version"],
                record.quality_status,
                canonical_json(record),
            ),
        ).rowcount
        if added:
            specs = FIELDS[record.key.statement_type]
            columns = ",".join(spec.name for spec in specs)
            marks = ",".join("?" for _ in range(len(specs) + 1))
            self.conn.execute(
                f"INSERT INTO clean_{record.key.statement_type}(clean_version_id,{columns}) VALUES({marks})",
                (record.clean_version_id, *(record.values.get(spec.name) for spec in specs)),
            )
        return bool(added)

    def clean(self, source_id: str, profile: dict) -> CleanRecord | None:
        row = self.conn.execute(
            "SELECT payload_json FROM clean_versions WHERE source_version_id=? AND mapping_version=? AND cleaning_rule_version=?",
            (source_id, profile["mapping_version"], profile["cleaning_rule_version"]),
        ).fetchone()
        return decode_clean(row[0]) if row else None

    def save_feature(
        self, feature: FeatureResult, scope_id: str, profile_id: str, computed_at: str
    ) -> bool:
        added = self.conn.execute(
            "INSERT OR IGNORE INTO feature_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                feature.feature_version_id,
                scope_id,
                profile_id,
                feature.security_code,
                feature.report_end,
                feature.feature_name,
                feature.feature_value,
                feature.feature_status,
                feature.data_available_at,
                computed_at,
                feature.input_hash,
                canonical_json(feature),
            ),
        ).rowcount
        if added:
            for number, entry in enumerate(feature.inputs):
                self.conn.execute(
                    "INSERT INTO feature_inputs VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        feature.feature_version_id,
                        number,
                        key_text(entry.key),
                        entry.record_state_id,
                        entry.clean_version_id,
                        entry.field_name,
                        entry.input_role,
                        entry.input_available_at,
                        canonical_json(entry),
                    ),
                )
        return bool(added)

    def snapshot(self, snapshot_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM dataset_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if not row:
            raise ValueError("快照不存在")
        return row

    def select_snapshot(
        self,
        scope_id: str | None = None,
        snapshot_id: str | None = None,
        as_of: str | None = None,
        allow_stale: bool = False,
    ) -> sqlite3.Row:
        """先固定完整发布快照，当前受阻默认拒绝，历史审计不回算。"""
        if snapshot_id:
            row = self.snapshot(snapshot_id)
            if scope_id and row["scope_id"] != scope_id:
                raise ValueError("指定快照不属于请求范围")
            if row["status"] != "PUBLISHED":
                raise ValueError("候选尚未发布，不能消费")
            if as_of and row["published_at"] > as_of:
                raise ValueError("指定快照在请求时点尚未发布")
            return row
        scope = self.scope(scope_id)
        if not as_of and scope["health_status"] != "healthy" and not allow_stale:
            raise ValueError(f"blocked/stale：{scope['health_reason']}")
        if as_of:
            row = self.conn.execute(
                "SELECT * FROM dataset_snapshots WHERE scope_id=? AND status='PUBLISHED' AND published_at<=? ORDER BY publication_sequence DESC LIMIT 1",
                (scope["scope_id"], as_of),
            ).fetchone()
        else:
            head = self.head(scope["scope_id"])
            row = self.snapshot(head) if head else None
        if not row:
            raise ValueError("指定时点没有已发布快照")
        if not as_of and not allow_stale:
            from finpipe.publication import snapshot_sources_current

            if not snapshot_sources_current(self, row["snapshot_id"]):
                raise ValueError("blocked/stale：来源已更新，该范围尚未按最新事实重建")
        return row

    def features(
        self,
        snapshot_id: str,
        company: str | None = None,
        period: str | None = None,
        name: str | None = None,
    ) -> list[FeatureResult]:
        """用成员主键筛选固定快照，避免查询一家时读取全部因子。"""
        filters, parameters = ["m.snapshot_id=?"], [snapshot_id]
        for column, value in (
            ("security_code", company),
            ("report_end", period),
            ("feature_name", name),
        ):
            if value is not None:
                filters.append(f"m.{column}=?")
                parameters.append(value)
        return [
            decode_feature(row[0])
            for row in self.conn.execute(
                "SELECT f.payload_json FROM snapshot_features m JOIN feature_versions f USING(feature_version_id) WHERE "
                + " AND ".join(filters)
                + " ORDER BY m.security_code,m.report_end,m.feature_name",
                parameters,
            )
        ]

    def activate(self, snapshot_id: str, manifest_hash: str, exported_at: str) -> bool:
        """在同事务比较父head并激活；过候选只审计，不制造生产发布时间。"""
        from finpipe.exports import sha256, validate_files
        from finpipe.publication import snapshot_sources_current, validate_snapshot

        validate_snapshot(self, snapshot_id)
        directory = self.workspace / "snapshots" / snapshot_id
        manifest = validate_files(directory, snapshot_id)
        snapshot = self.snapshot(snapshot_id)
        if (
            sha256(directory / "manifest.json") != manifest_hash
            or snapshot["manifest_hash"] != manifest_hash
            or manifest["exported_at"] != exported_at
        ):
            raise ValueError("激活必须引用已锚定的原封存清单")
        with self.transaction():
            row = self.snapshot(snapshot_id)
            if row["status"] == "PUBLISHED":
                return True
            if row["status"] != "PREPARED":
                return False
            if not snapshot_sources_current(self, snapshot_id):
                self.conn.execute(
                    "UPDATE dataset_snapshots SET status='SUPERSEDED' WHERE snapshot_id=?",
                    (snapshot_id,),
                )
                return False
            if self.head(row["scope_id"]) != row["expected_parent_head"]:
                self.conn.execute(
                    "UPDATE dataset_snapshots SET status='SUPERSEDED' WHERE snapshot_id=?",
                    (snapshot_id,),
                )
                return False
            sequence = self.conn.execute(
                "SELECT COALESCE(MAX(publication_sequence),0)+1 FROM dataset_snapshots WHERE scope_id=?",
                (row["scope_id"],),
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE dataset_snapshots SET status='PUBLISHED',manifest_hash=?,exported_at=?,published_at=?,publication_sequence=? WHERE snapshot_id=?",
                (manifest_hash, exported_at, utc_now(), sequence, snapshot_id),
            )
            self.conn.execute(
                "INSERT INTO workspace_head VALUES(?,?) ON CONFLICT(scope_id) DO UPDATE SET snapshot_id=excluded.snapshot_id",
                (row["scope_id"], snapshot_id),
            )
        return True
