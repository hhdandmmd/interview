-- SQLite为唯一权威；数据版本不可变，运行进度与发布事实分开。
PRAGMA foreign_keys = ON;
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version VALUES(1);
CREATE TABLE processing_profiles(profile_id TEXT PRIMARY KEY, content_json TEXT NOT NULL);
CREATE TABLE universe_snapshots(universe_id TEXT PRIMARY KEY, content_json TEXT NOT NULL);
CREATE TABLE scopes(
    scope_id TEXT PRIMARY KEY,
    universe_id TEXT NOT NULL REFERENCES universe_snapshots,
    capture_kind TEXT NOT NULL CHECK(capture_kind IN ('real','synthetic')),
    content_json TEXT NOT NULL,
    health_status TEXT NOT NULL DEFAULT 'healthy',
    health_reason TEXT,
    last_success_at TEXT
);
CREATE TABLE runs(
    run_id TEXT PRIMARY KEY,
    scope_id TEXT REFERENCES scopes,
    profile_id TEXT NOT NULL REFERENCES processing_profiles,
    parent_run_id TEXT REFERENCES runs,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT,
    plan_json TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    snapshot_id TEXT,
    error_json TEXT
);
CREATE TABLE expected_keys(
    scope_id TEXT NOT NULL REFERENCES scopes,
    business_key TEXT NOT NULL,
    eligibility TEXT NOT NULL CHECK(eligibility IN ('eligible','unknown','not_required')),
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(scope_id,business_key)
);
CREATE TABLE batches(
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs,
    status TEXT NOT NULL,
    content_json TEXT NOT NULL
);
CREATE TABLE http_attempts(
    attempt_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches,
    page_number INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    observed_at TEXT,
    body_path TEXT,
    raw_hash TEXT,
    content_json TEXT NOT NULL
);
CREATE VIEW source_pages AS SELECT * FROM http_attempts WHERE body_path IS NOT NULL;
CREATE TABLE source_versions(
    source_version_id TEXT PRIMARY KEY,
    business_key TEXT NOT NULL,
    source TEXT NOT NULL,
    security_code TEXT NOT NULL,
    report_end TEXT NOT NULL,
    statement_type TEXT NOT NULL,
    consolidation_scope TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX source_business ON source_versions(business_key);
CREATE TABLE source_observations(
    observation_id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL REFERENCES source_versions,
    attempt_id TEXT NOT NULL REFERENCES http_attempts,
    row_index INTEGER NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE run_inputs(
    run_id TEXT NOT NULL REFERENCES runs,
    observation_id TEXT NOT NULL REFERENCES source_observations,
    PRIMARY KEY(run_id,observation_id)
);
CREATE TABLE clean_versions(
    clean_version_id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL REFERENCES source_versions,
    mapping_version TEXT NOT NULL,
    cleaning_rule_version TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(source_version_id,mapping_version,cleaning_rule_version)
);
CREATE TABLE record_states(
    record_state_id TEXT PRIMARY KEY,
    business_key TEXT NOT NULL,
    state_sequence INTEGER NOT NULL,
    state_status TEXT NOT NULL CHECK(state_status IN ('selected','conflict','missing')),
    selected_source_version_id TEXT REFERENCES source_versions,
    decision_observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(business_key,state_sequence),
    CHECK((state_status='selected' AND selected_source_version_id IS NOT NULL) OR
          (state_status!='selected' AND selected_source_version_id IS NULL))
);
CREATE INDEX state_business ON record_states(business_key,state_sequence DESC);
CREATE TABLE feature_versions(
    feature_version_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES scopes,
    profile_id TEXT NOT NULL REFERENCES processing_profiles,
    security_code TEXT NOT NULL,
    report_end TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    feature_value REAL,
    feature_status TEXT NOT NULL,
    data_available_at TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(scope_id,profile_id,security_code,report_end,feature_name,input_hash),
    CHECK((feature_value IS NOT NULL AND feature_status IN ('ok','warning')) OR
          (feature_value IS NULL AND feature_status NOT IN ('ok','warning')))
);
CREATE INDEX feature_target ON feature_versions(scope_id,security_code,report_end,feature_name);
CREATE TABLE feature_inputs(
    feature_version_id TEXT NOT NULL REFERENCES feature_versions,
    input_number INTEGER NOT NULL,
    business_key TEXT NOT NULL,
    record_state_id TEXT REFERENCES record_states,
    clean_version_id TEXT REFERENCES clean_versions,
    field_name TEXT NOT NULL,
    input_role TEXT NOT NULL,
    input_available_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(feature_version_id,input_number)
);
CREATE INDEX input_reverse ON feature_inputs(business_key,field_name);
CREATE TABLE quality_events(
    event_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE quality_metrics(
    run_id TEXT NOT NULL REFERENCES runs,
    metric_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,metric_name)
);
CREATE TABLE alerts(
    alert_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES quality_events,
    scope_id TEXT REFERENCES scopes,
    status TEXT NOT NULL CHECK(status IN ('open','acknowledged','resolved')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    recovery_run_id TEXT REFERENCES runs,
    payload_json TEXT NOT NULL
);
CREATE TABLE dataset_snapshots(
    snapshot_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES scopes,
    universe_id TEXT NOT NULL REFERENCES universe_snapshots,
    profile_id TEXT NOT NULL REFERENCES processing_profiles,
    run_id TEXT NOT NULL REFERENCES runs,
    expected_parent_head TEXT REFERENCES dataset_snapshots,
    data_as_of TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PREPARED','PUBLISHED','SUPERSEDED','ABORTED')),
    signature TEXT NOT NULL,
    manifest_hash TEXT,
    exported_at TEXT,
    published_at TEXT,
    publication_sequence INTEGER,
    UNIQUE(scope_id,publication_sequence)
);
CREATE INDEX snapshot_history ON dataset_snapshots(scope_id,status,publication_sequence DESC);
CREATE TABLE snapshot_records(
    snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots,
    business_key TEXT NOT NULL,
    source TEXT NOT NULL,
    security_code TEXT NOT NULL,
    report_end TEXT NOT NULL,
    statement_type TEXT NOT NULL,
    consolidation_scope TEXT NOT NULL,
    record_state_id TEXT NOT NULL REFERENCES record_states,
    selected_clean_version_id TEXT REFERENCES clean_versions,
    data_available_at TEXT NOT NULL,
    source_available_at TEXT,
    PRIMARY KEY(snapshot_id,business_key)
);
CREATE TABLE snapshot_features(
    snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots,
    security_code TEXT NOT NULL,
    report_end TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    feature_version_id TEXT NOT NULL REFERENCES feature_versions,
    PRIMARY KEY(snapshot_id,security_code,report_end,feature_name)
);
CREATE TABLE workspace_head(
    scope_id TEXT PRIMARY KEY REFERENCES scopes,
    snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots
);
CREATE TRIGGER published_snapshot_immutable BEFORE UPDATE ON dataset_snapshots
WHEN OLD.status='PUBLISHED' BEGIN SELECT RAISE(ABORT,'已发布快照不可修改'); END;
CREATE TRIGGER head_requires_published BEFORE INSERT ON workspace_head
WHEN NOT EXISTS(SELECT 1 FROM dataset_snapshots
                WHERE snapshot_id=NEW.snapshot_id AND scope_id=NEW.scope_id AND status='PUBLISHED')
BEGIN SELECT RAISE(ABORT,'发布头必须引用同范围已发布快照'); END;
CREATE TRIGGER head_update_requires_published BEFORE UPDATE ON workspace_head
WHEN NOT EXISTS(SELECT 1 FROM dataset_snapshots
                WHERE snapshot_id=NEW.snapshot_id AND scope_id=NEW.scope_id AND status='PUBLISHED')
BEGIN SELECT RAISE(ABORT,'发布头必须引用同范围已发布快照'); END;
