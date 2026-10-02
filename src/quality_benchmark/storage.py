"""在基础数据库上扩展质量对标平台的表结构。"""

from __future__ import annotations

from pathlib import Path

from beverage_ops_foundation.storage import Database


QUALITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_versions (
    metric_version_id TEXT PRIMARY KEY,
    metric_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('higher_better', 'lower_better')),
    weight REAL NOT NULL CHECK(weight > 0),
    applicable_products_json TEXT NOT NULL,
    sampling_window_json TEXT NOT NULL,
    method_version TEXT NOT NULL,
    adjustment_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'active', 'retired')),
    supersedes_version_id TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(metric_id, version)
);
CREATE TABLE IF NOT EXISTS ranking_cycles (
    cycle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'frozen', 'published')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS cycle_metrics (
    cycle_id TEXT NOT NULL REFERENCES ranking_cycles(cycle_id),
    metric_id TEXT NOT NULL,
    metric_version_id TEXT NOT NULL REFERENCES metric_versions(metric_version_id),
    PRIMARY KEY (cycle_id, metric_id)
);
CREATE TABLE IF NOT EXISTS evidence_submissions (
    submission_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES ranking_cycles(cycle_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    metric_id TEXT NOT NULL,
    measurements_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(cycle_id, site_id, metric_id)
);
CREATE TABLE IF NOT EXISTS score_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES ranking_cycles(cycle_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    metric_id TEXT NOT NULL,
    metric_version_id TEXT NOT NULL REFERENCES metric_versions(metric_version_id),
    raw_score REAL NOT NULL,
    adjusted_score REAL NOT NULL,
    computation_json TEXT NOT NULL,
    excluded_batches_json TEXT NOT NULL DEFAULT '[]',
    excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0, 1)),
    suspended INTEGER NOT NULL DEFAULT 0 CHECK(suspended IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(cycle_id, site_id, metric_id)
);
CREATE TABLE IF NOT EXISTS exclusion_requests (
    exclusion_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    batch_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
    requested_by TEXT NOT NULL REFERENCES actors(actor_id),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES actors(actor_id),
    reviewed_at TEXT,
    review_note TEXT,
    impact_json TEXT
);
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved_upheld', 'resolved_rejected')),
    raised_by TEXT NOT NULL REFERENCES actors(actor_id),
    raised_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES actors(actor_id),
    resolved_at TEXT,
    resolution_note TEXT
);
CREATE TABLE IF NOT EXISTS metric_publications (
    publication_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('published', 'suspended')),
    reason TEXT,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(cycle_id, metric_id, version)
);
CREATE TABLE IF NOT EXISTS publication_entries (
    publication_id TEXT NOT NULL REFERENCES metric_publications(publication_id),
    site_id TEXT NOT NULL,
    adjusted_score REAL,
    rank_position INTEGER,
    included INTEGER NOT NULL CHECK(included IN (0, 1)),
    PRIMARY KEY (publication_id, site_id)
);
CREATE TABLE IF NOT EXISTS total_publications (
    publication_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(cycle_id, version)
);
CREATE TABLE IF NOT EXISTS total_entries (
    publication_id TEXT NOT NULL REFERENCES total_publications(publication_id),
    site_id TEXT NOT NULL,
    total_score REAL NOT NULL,
    rank_position INTEGER NOT NULL,
    PRIMARY KEY (publication_id, site_id)
);
CREATE TABLE IF NOT EXISTS corrective_plans (
    plan_id TEXT PRIMARY KEY,
    finding_type TEXT NOT NULL CHECK(finding_type IN ('metric_result', 'dispute', 'exclusion')),
    finding_id TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    description TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'submitted', 'verified')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    verified_by TEXT REFERENCES actors(actor_id),
    verified_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_evidence (
    evidence_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES corrective_plans(plan_id),
    description TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permission_grants (
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    permission TEXT NOT NULL,
    granted_by TEXT NOT NULL REFERENCES actors(actor_id),
    granted_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, permission)
);
CREATE TABLE IF NOT EXISTS release_decisions (
    decision_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('released', 'rejected')),
    basis_json TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES actors(actor_id),
    decided_at TEXT NOT NULL,
    UNIQUE(site_id, batch_id)
);
"""


class QualityDatabase(Database):
    """在基础服务库结构上追加质量对标平台表。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(QUALITY_SCHEMA)
