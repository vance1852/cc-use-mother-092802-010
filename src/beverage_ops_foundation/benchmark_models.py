"""定义跨工厂质量对标领域的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class MetricDefinition:
    """版本化的质量指标定义（采样窗口、检测方法换版、可比性调整）。"""

    metric_id: str
    version: int
    code: str
    name: str
    direction: str  # higher_better | lower_better
    weight: float
    applicable_products: tuple[str, ...]
    sampling: dict[str, Any]
    method_code: str
    method_version: str
    rule: dict[str, Any]
    adjustment: dict[str, Any]
    status: str  # draft | active | retired
    created_by: str
    created_at: str


@dataclass(frozen=True)
class BenchmarkCycle:
    """一次周期性对标（填报期冻结后证据与计算结果不可变）。"""

    cycle_id: str
    code: str
    period_start: str
    period_end: str
    status: str  # open | frozen | published | closed
    frozen_at: Optional[str]
    published_at: Optional[str]


@dataclass(frozen=True)
class Evidence:
    """工厂提交的单批次检测证据。"""

    evidence_id: str
    cycle_id: str
    site_id: str
    metric_id: str
    batch_no: str
    product_code: str
    method_version: str
    sampled_at: str
    raw_value: float
    payload: dict[str, Any]
    evidence_hash: str
    excluded: bool
    submitted_by: str
    created_at: str


@dataclass(frozen=True)
class ExclusionRequest:
    """异常值排除申请，必须由独立人员审查。"""

    exclusion_id: str
    cycle_id: str
    site_id: str
    metric_id: str
    evidence_id: str
    reason: str
    status: str  # pending | approved | rejected
    requested_by: str
    requested_at: str
    reviewed_by: Optional[str]
    reviewed_at: Optional[str]
    review_note: Optional[str]


@dataclass(frozen=True)
class RankImpact:
    """排除前后对名次与分数的影响快照。"""

    scope: str  # metric | total
    site_id: str
    rank_before: Optional[int]
    score_before: Optional[float]
    rank_after: Optional[int]
    score_after: Optional[float]


@dataclass(frozen=True)
class FrozenScore:
    """冻结时刻的单工厂单指标计算结果与规则版本。"""

    cycle_id: str
    site_id: str
    metric_id: str
    metric_version: int
    score: Optional[float]
    adjustment_factor: float
    calc: dict[str, Any]
    rank_metric: Optional[int]


@dataclass(frozen=True)
class Dispute:
    """工厂对结果提出的异议，仅暂停争议指标的发布。"""

    dispute_id: str
    cycle_id: str
    site_id: str
    metric_id: str
    reason: str
    status: str  # open | resolved_upheld | resolved_corrected | withdrawn


@dataclass(frozen=True)
class Publication:
    """一次发布清单快照（争议指标标记 held_disputed，其余正常发布）。"""

    publication_id: str
    cycle_id: str
    version: int
    manifest: dict[str, Any]
    manifest_hash: str
    published_by: str
    published_at: str


@dataclass(frozen=True)
class Finding:
    """排名或检测中发现的具体问题，是整改计划的挂载点。"""

    finding_id: str
    cycle_id: str
    site_id: str
    metric_id: Optional[str]
    metric_version: Optional[int]
    source: str  # ranking | exclusion | dispute | audit
    description: str
    severity: str  # low | medium | high
    created_by: str
    created_at: str


@dataclass(frozen=True)
class RectificationPlan:
    """整改计划，关联发现、责任人、期限和复验证据。"""

    plan_id: str
    finding_id: str
    owner_actor_id: str
    due_date: str
    status: str  # open | verification_submitted | closed
    created_by: str
    created_at: str
    closed_at: Optional[str]


@dataclass(frozen=True)
class BatchRelease:
    """产品批次放行决定，与排名权限严格分离。"""

    release_id: str
    site_id: str
    batch_no: str
    product_code: str
    decision: str  # released | rejected | held
    reason: str
    decided_by: str
    decided_at: str


# 批次放行角色与排名角色严格分离（quality_manager/reviewer/operator 均不在其中）
RELEASE_ROLES = frozenset({"admin", "release_officer"})
# 有权独立审查异常值排除与整改复验的角色（且不得来自被审工厂组织）
INDEPENDENT_REVIEW_ROLES = frozenset({"admin", "reviewer"})
