"""从一个固定候选导出类型化产物；封存清单不预写实际发布时间。"""

import hashlib
import html
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from finpipe.catalog import FIELDS, field_dictionary
from finpipe.features import feature_dictionary
from finpipe.identity import canonical_json
from finpipe.storage.database import Store, decode_clean
from finpipe.timeutil import utc_now

REQUIRED_FILES = {
    *(
        name + "." + extension
        for name in ("income", "balance", "indicator", "features_long", "features_panel")
        for extension in ("csv", "parquet")
    ),
    "dictionary.json",
    "dictionary.md",
    "quality.json",
    "report.html",
}


def write_json(path: Path, value: dict):
    """原子写派生JSON，临时文件和目标在同一目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    """读取固定文件摘要，不能据文件名推断完整。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frames(store: Store, snapshot_id: str) -> dict[str, pd.DataFrame]:
    """精确引用成员生成规范表、长表与全网格面板。"""
    snapshot = store.snapshot(snapshot_id)
    scope = json.loads(store.scope(snapshot["scope_id"])["content_json"])
    financial = {name: [] for name in FIELDS}
    for row in store.conn.execute(
        "SELECT r.*,s.payload_json AS state_json,c.payload_json AS clean_json "
        "FROM snapshot_records r JOIN record_states s USING(record_state_id) "
        "LEFT JOIN clean_versions c ON c.clean_version_id=r.selected_clean_version_id "
        "WHERE r.snapshot_id=? ORDER BY r.security_code,r.report_end,r.statement_type",
        (snapshot_id,),
    ):
        clean = decode_clean(row["clean_json"]) if row["clean_json"] else None
        state = json.loads(row["state_json"])
        entry = {
            "source": row["source"],
            "security_code": row["security_code"],
            "report_end": row["report_end"],
            "consolidation_scope": row["consolidation_scope"],
            "record_state_id": row["record_state_id"],
            "clean_version_id": row["selected_clean_version_id"],
            "record_status": state["state_status"],
            "quality_status": clean.quality_status if clean else state["state_status"],
            "data_available_at": row["data_available_at"],
            "source_available_at": row["source_available_at"],
            "field_states": canonical_json(clean.field_states if clean else {}),
            "reason_codes": canonical_json(
                clean.reasons if clean else {"record": [state["reason"]]}
            ),
        }
        entry.update(
            {
                spec.name: clean.values.get(spec.name) if clean else None
                for spec in FIELDS[row["statement_type"]]
            }
        )
        financial[row["statement_type"]].append(entry)
    long = []
    for feature in store.features(snapshot_id):
        entry = asdict(feature)
        entry.pop("inputs")
        entry["reason_codes"] = canonical_json(entry["reason_codes"])
        entry["evidence_flags"] = canonical_json(entry["evidence_flags"])
        entry.update(
            {
                "snapshot_id": snapshot_id,
                "scope_id": snapshot["scope_id"],
                "universe_id": snapshot["universe_id"],
                "profile_id": snapshot["profile_id"],
                "data_as_of": snapshot["data_as_of"],
            }
        )
        entry["computed_at"] = store.conn.execute(
            "SELECT computed_at FROM feature_versions WHERE feature_version_id=?",
            (feature.feature_version_id,),
        ).fetchone()[0]
        long.append(entry)
    feature_frame = pd.DataFrame(long)
    panel = feature_frame.pivot(
        index=["security_code", "report_end"], columns="feature_name", values="feature_value"
    )
    grid = pd.MultiIndex.from_product(
        [scope["codes"], scope["output_periods"]], names=["security_code", "report_end"]
    )
    panel = panel.reindex(grid).reset_index()
    panel.columns.name = None
    result = {name: pd.DataFrame(rows) for name, rows in financial.items()}
    result.update(features_long=feature_frame, features_panel=panel)
    return result


def typed_table(frame: pd.DataFrame, numeric: set[str], metadata: dict) -> pa.Table:
    """显式覆盖日期、UTC时间和数值类型，避免全NULL列变成无类型。"""
    frame = frame.copy()
    fields = []
    for name in frame.columns:
        if name in numeric:
            frame[name] = pd.to_numeric(frame[name], errors="raise").astype("float64")
            dtype = pa.float64()
        elif name == "report_end":
            frame[name] = pd.to_datetime(frame[name]).dt.date
            dtype = pa.date32()
        elif name.endswith("_at") or name == "data_as_of":
            frame[name] = pd.to_datetime(frame[name], utc=True)
            dtype = pa.timestamp("us", tz="UTC")
        else:
            frame[name] = frame[name].astype("string")
            dtype = pa.string()
        fields.append(pa.field(name, dtype, nullable=True))
    schema = pa.schema(fields, metadata={b"finpipe": canonical_json(metadata).encode("utf-8")})
    return pa.Table.from_pandas(frame, schema=schema, preserve_index=False)


def export_candidate(store: Store, snapshot_id: str, quality: dict) -> Path:
    """生成固定候选所有文件，回读后写最后一份封存清单。"""
    snapshot = store.snapshot(snapshot_id)
    directory = store.workspace / "staging" / snapshot_id
    directory.mkdir(parents=True, exist_ok=True)
    profile = store.profile(snapshot["profile_id"])
    datasets = frames(store, snapshot_id)
    files = []
    feature_names = set(datasets["features_long"]["feature_name"])
    for name, frame in datasets.items():
        numeric = {spec.name for specs in FIELDS.values() for spec in specs} | {"feature_value"}
        if name == "features_panel":
            numeric |= feature_names
        metadata = {
            key: snapshot[key] for key in ("snapshot_id", "scope_id", "profile_id", "universe_id")
        }
        table = typed_table(frame, numeric, metadata)
        parquet = directory / f"{name}.parquet"
        pq.write_table(table, parquet, compression="zstd", row_group_size=65536)
        if not pq.read_table(parquet).equals(table):
            raise ValueError(f"Parquet回读与候选不一致：{name}")
        csv = directory / f"{name}.csv"
        frame.to_csv(csv, index=False, encoding="utf-8-sig", na_rep="")
        for path in (parquet, csv):
            files.append(
                {
                    "path": path.name,
                    "sha256": sha256(path),
                    "rows": len(frame),
                    "bytes": path.stat().st_size,
                    "format": path.suffix[1:],
                }
            )
    database_dictionary = {}
    descriptions = {
        "source_version_id": "不可变来源版本身份",
        "clean_version_id": "精确清洗处理身份",
        "record_state_id": "业务裁决事件身份",
        "feature_version_id": "精确特征版本身份",
        "scope_id": "冻结样本及输入输出范围",
        "universe_id": "冻结公司池身份",
        "profile_id": "映射、规则、公式、字典及时间政策内容指纹",
        "run_id": "运行身份",
        "security_code": "保留交易所及前导零的证券代码",
        "report_end": "自然年报告季末",
        "business_key": "来源、公司、报告期、报表与合并范围的完整键",
        "data_available_at": "冻结的真实输入或失效状态可用边界",
        "published_at": "SQL实际激活时间",
        "source_available_at": "来源公告及更新的保守边界，坏证据允许空",
        "payload_json": "本对象的完整值、状态和证据",
        "content_json": "不可变对象规格及依据",
        "raw_json": "来源行内容，原HTTP正文另行归档",
        "input_hash": "精确输入、状态、期间和边界摘要",
        "expected_parent_head": "激活事务必须匹配的父发布指针，首次允许空",
    }
    for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        table_name = row[0]
        columns = []
        for column in store.conn.execute(f'PRAGMA table_info("{table_name}")'):
            columns.append(
                {
                    "name": column["name"],
                    "dtype": column["type"],
                    "nullable": not bool(column["notnull"]),
                    "primary_key_position": column["pk"],
                    "description": descriptions.get(
                        column["name"], f"{table_name}的{column['name']}字段，语义按对应对象契约"
                    ),
                }
            )
        database_dictionary[table_name] = {
            "owner": "finpipe.storage",
            "columns": columns,
            "foreign_keys": [
                dict(key) for key in store.conn.execute(f'PRAGMA foreign_key_list("{table_name}")')
            ],
        }
    dictionary = {
        "dictionary_version": profile["dictionary_version"],
        "fields": field_dictionary(),
        "features": feature_dictionary(profile["formula_version"]),
        "database": database_dictionary,
        "profile": profile,
        "states": {
            "record": ["selected", "conflict", "missing"],
            "field": ["ok", "warning", "missing", "not_applicable", "invalid"],
            "feature": [
                "ok",
                "warning",
                "missing_input",
                "missing_history",
                "invalid_denominator",
                "nonpositive_baseline",
                "not_applicable",
                "input_error",
                "version_conflict",
            ],
        },
        "null_policy": "真实空值须带原因，零与NULL分开；_pp存比例差，展示乘100为百分点",
        "dataset_columns": {
            name: str(
                typed_table(
                    frame,
                    {spec.name for specs in FIELDS.values() for spec in specs}
                    | {"feature_value"}
                    | (feature_names if name == "features_panel" else set()),
                    {},
                ).schema
            )
            for name, frame in datasets.items()
        },
    }
    write_json(directory / "dictionary.json", dictionary)
    (directory / "dictionary.md").write_text(
        "# 本快照数据字典\n\n"
        + "\n".join(
            f"- {name}: {spec.description}；单位 {spec.unit}；期间 {spec.period_basis}"
            for specs in FIELDS.values()
            for spec in specs
            for name in [spec.name]
        )
        + "\n\n## 财务特征\n\n"
        + "\n".join(
            f"- {spec['name']}: {spec.get('description', '')}；{spec.get('formula', '')}；单位 {spec.get('storage_unit', '')}"
            for spec in feature_dictionary(profile["formula_version"])
        )
        + "\n\n## 持久表\n\n"
        + "\n".join(
            f"- {name}: " + "、".join(column["name"] for column in spec["columns"])
            for name, spec in database_dictionary.items()
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(directory / "quality.json", quality)
    kind = store.scope(snapshot["scope_id"])["capture_kind"]
    report = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>财报质量报告</title><style>body{font:16px sans-serif;margin:32px;color:#16324a}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ccd}pre{white-space:pre-wrap}</style>"
    report += f"<h1>财报处理报告</h1><p>快照：{html.escape(snapshot_id)}；数据身份：{kind}</p>"
    report += "<p>当前取得的修订财报不构成过去真实投资信号。NULL和警告保留。</p>"
    metrics = quality["metrics"]
    summary = pd.DataFrame(
        [
            (label, metrics.get(key))
            for key, label in (
                ("planned_count", "预期报表"),
                ("received_count", "实际报表"),
                ("usable_report_rate", "保守可用率"),
                ("field_null_rate", "已有字段空值率"),
                ("feature_count", "特征位置"),
                ("feature_value_count", "有数值特征"),
            )
        ],
        columns=["指标", "结果"],
    )
    report += "<h2>质量摘要</h2>" + summary.to_html(index=False, escape=True)
    report += "<h2>因子样例</h2>" + datasets["features_long"][
        ["security_code", "report_end", "feature_name", "feature_value", "feature_status"]
    ].head(30).to_html(index=False, escape=True, na_rep="未满足输入")
    report += (
        "<h2>完整产物</h2><ul>"
        + "".join(
            f'<li><a href="{name}">{label}</a></li>'
            for name, label in (
                ("features_long.csv", "完整因子长表CSV"),
                ("features_panel.parquet", "完整因子面板Parquet"),
                ("quality.json", "完整质量与核对明细JSON"),
                ("dictionary.md", "字段与公式字典"),
            )
        )
        + "</ul>"
    )
    (directory / "report.html").write_text(report + "</html>", encoding="utf-8")
    for filename in ("dictionary.json", "dictionary.md", "quality.json", "report.html"):
        path = directory / filename
        files.append(
            {
                "path": filename,
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
                "format": path.suffix[1:],
            }
        )
    manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "scope_id": snapshot["scope_id"],
        "profile_id": snapshot["profile_id"],
        "universe_id": snapshot["universe_id"],
        "expected_parent_head": snapshot["expected_parent_head"],
        "data_as_of": snapshot["data_as_of"],
        "exported_at": utc_now(),
        "source_capture_kind": kind,
        "revision_history_verified": False,
        "files": files,
        "quality": quality,
    }
    write_json(directory / "manifest.json", manifest)
    validate_files(directory, snapshot_id)
    return directory


def validate_files(directory: Path, snapshot_id: str) -> dict:
    """封存产物逐项核验，拒绝路径越界或只看seal标记。"""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    names = [entry["path"] for entry in manifest.get("files", [])]
    if (
        manifest["snapshot_id"] != snapshot_id
        or set(names) != REQUIRED_FILES
        or len(names) != len(REQUIRED_FILES)
    ):
        raise ValueError("发布清单身份或文件集合无效")
    for entry in manifest["files"]:
        path = (directory / entry["path"]).resolve()
        if (
            not path.is_relative_to(directory.resolve())
            or not path.is_file()
            or sha256(path) != entry["sha256"]
        ):
            raise ValueError(f"产物缺失或摘要不一致：{entry['path']}")
    return manifest


def publication_receipt(store: Store, snapshot_id: str):
    """只从已提交SQLite事实派生回执，不修改封存manifest。"""
    snapshot = store.snapshot(snapshot_id)
    if snapshot["status"] != "PUBLISHED":
        raise ValueError("未发布候选不能生成发布回执")
    write_json(
        store.workspace / "snapshots" / snapshot_id / "publication_receipt.json",
        {
            "snapshot_id": snapshot_id,
            "scope_id": snapshot["scope_id"],
            "manifest_hash": snapshot["manifest_hash"],
            "published_at": snapshot["published_at"],
            "publication_sequence": snapshot["publication_sequence"],
        },
    )
