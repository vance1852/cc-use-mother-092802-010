"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_definitions (
    metric_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('higher_better','lower_better')),
    weight REAL NOT NULL CHECK(weight >= 0),
    applicable_products_json TEXT NOT NULL,
    sampling_json TEXT NOT NULL,
    method_code TEXT NOT NULL,
    method_version TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    adjustment_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (metric_id, version)
);
CREATE TABLE IF NOT EXISTS benchmark_cycles (
    cycle_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','frozen','published','closed')),
    frozen_at TEXT,
    published_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycle_metrics (
    cycle_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    metric_version INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, metric_id),
    FOREIGN KEY (metric_id, metric_version) REFERENCES metric_definitions(metric_id, version)
);
CREATE TABLE IF NOT EXISTS cycle_sites (
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, site_id),
    FOREIGN KEY (site_id) REFERENCES sites(site_id)
);
CREATE TABLE IF NOT EXISTS cycle_adjustments (
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    factor REAL NOT NULL CHECK(factor > 0),
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, site_id, metric_id)
);
CREATE TABLE IF NOT EXISTS metric_evidence (
    evidence_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    product_code TEXT NOT NULL,
    method_version TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    raw_value REAL NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0, 1)),
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(cycle_id, site_id, metric_id, batch_no),
    FOREIGN KEY (cycle_id, metric_id) REFERENCES cycle_metrics(cycle_id, metric_id),
    FOREIGN KEY (cycle_id, site_id) REFERENCES cycle_sites(cycle_id, site_id)
);
CREATE TABLE IF NOT EXISTS exclusion_requests (
    exclusion_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES metric_evidence(evidence_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT
);
CREATE TABLE IF NOT EXISTS exclusion_rank_impacts (
    impact_id TEXT PRIMARY KEY,
    exclusion_id TEXT NOT NULL REFERENCES exclusion_requests(exclusion_id),
    scope TEXT NOT NULL CHECK(scope IN ('metric','total')),
    site_id TEXT NOT NULL,
    rank_before INTEGER,
    score_before REAL,
    rank_after INTEGER,
    score_after REAL,
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frozen_scores (
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    metric_version INTEGER NOT NULL,
    score REAL,
    adjustment_factor REAL NOT NULL,
    calc_json TEXT NOT NULL,
    rank_metric INTEGER,
    frozen_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, site_id, metric_id)
);
CREATE TABLE IF NOT EXISTS metric_disputes (
    dispute_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved_upheld','resolved_corrected','withdrawn')),
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT,
    UNIQUE(cycle_id, site_id, metric_id)
);
CREATE TABLE IF NOT EXISTS score_corrections (
    correction_id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL UNIQUE REFERENCES metric_disputes(dispute_id),
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    original_score REAL NOT NULL,
    corrected_score REAL NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycle_publications (
    publication_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES benchmark_cycles(cycle_id),
    version INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(cycle_id, version)
);
CREATE TABLE IF NOT EXISTS published_scores (
    cycle_id TEXT NOT NULL,
    publication_version INTEGER NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    metric_version INTEGER NOT NULL,
    score REAL,
    rank_metric INTEGER,
    state TEXT NOT NULL CHECK(state IN ('published','held_disputed')),
    total_score REAL,
    rank_total INTEGER,
    PRIMARY KEY (cycle_id, publication_version, site_id, metric_id)
);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    metric_id TEXT,
    metric_version INTEGER,
    source TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rectification_plans (
    plan_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL UNIQUE REFERENCES findings(finding_id),
    owner_actor_id TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','verification_submitted','closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS rectification_verifications (
    verification_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES rectification_plans(plan_id),
    evidence_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    accepted INTEGER CHECK(accepted IN (0, 1)),
    review_note TEXT
);
CREATE TABLE IF NOT EXISTS batch_releases (
    release_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_no TEXT NOT NULL,
    product_code TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('released','rejected','held')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(site_id, batch_no)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
