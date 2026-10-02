"""质量指标评分、可比性调整与名次计算引擎。

评分结果完全由“指标定义版本 + 纳入证据 + 调整系数”推导，
计算过程以结构化 calc 明细落库，支持从任一分数回溯。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from .benchmark_models import MetricDefinition

# 支持的规则类型：
# mean          —— 对 raw_value 求均值，按 direction 与可选 scale 归一到 0..100
# pass_rate     —— payload["pass"] 为真的批次占比 * 100
# threshold_rate—— raw_value 落在 [lower, upper] 内的批次占比 * 100
RULE_TYPES = frozenset({"mean", "pass_rate", "threshold_rate"})


def validate_rule(definition: MetricDefinition) -> None:
    """在指标建档时校验计算规则自洽。"""

    rule = definition.rule or {}
    rule_type = rule.get("type")
    if rule_type not in RULE_TYPES:
        raise ValueError("rule.type 必须是 mean / pass_rate / threshold_rate")
    if rule_type == "threshold_rate":
        lower = rule.get("lower")
        upper = rule.get("upper")
        if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)) or lower > upper:
            raise ValueError("threshold_rate 规则需要合法的 lower/upper 边界")
    if rule_type == "mean" and "scale" in rule:
        if not isinstance(rule["scale"], (int, float)) or rule["scale"] <= 0:
            raise ValueError("scale 必须为正数")
    sampling = definition.sampling or {}
    if not isinstance(sampling, dict) or "basis" not in sampling:
        raise ValueError("sampling 必须包含采样依据 basis")
    if definition.weight < 0:
        raise ValueError("weight 不能为负")


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def score_cell(definition: MetricDefinition, evidence: Sequence[Any],
               adjustment_factor: float = 1.0) -> tuple[Optional[float], dict[str, Any]]:
    """计算单工厂单指标得分。

    evidence 中每项至少含 raw_value、payload、evidence_hash、excluded 字段；
    被排除的异常值不参与计算。无有效证据时得分为 None（该指标缺测）。
    """

    included = [item for item in evidence if not item.excluded]
    calc: dict[str, Any] = {
        "metric_id": definition.metric_id,
        "metric_version": definition.version,
        "method_code": definition.method_code,
        "method_version": definition.method_version,
        "rule": definition.rule,
        "adjustment_factor": adjustment_factor,
        "included_evidence": [item.evidence_id for item in included],
        "excluded_evidence": [item.evidence_id for item in evidence if item.excluded],
        "n": len(included),
    }
    if not included:
        calc["reason"] = "no_evidence"
        return None, calc

    rule = definition.rule
    rule_type = rule["type"]
    if rule_type == "mean":
        values = [float(item.raw_value) for item in included]
        aggregate = sum(values) / len(values)
        calc["aggregate"] = "mean"
        calc["raw_mean"] = round(aggregate, 6)
        if "scale" in rule:
            normalized = aggregate * 100.0 / float(rule["scale"])
            score = _clamp(normalized if definition.direction == "higher_better" else 100.0 - normalized)
        else:
            score = aggregate if definition.direction == "higher_better" else 100.0 - aggregate
    elif rule_type == "pass_rate":
        passed = sum(1 for item in included if bool(item.payload.get("pass")))
        calc["aggregate"] = "pass_rate"
        calc["passed"] = passed
        score = passed * 100.0 / len(included)
    else:
        lower = float(rule["lower"])
        upper = float(rule["upper"])
        passed = sum(1 for item in included if lower <= float(item.raw_value) <= upper)
        calc["aggregate"] = "threshold_rate"
        calc["passed"] = passed
        calc["bounds"] = [lower, upper]
        score = passed * 100.0 / len(included)

    score_before_adjustment = round(score, 4)
    adjusted = round(score * float(adjustment_factor), 4)
    calc["score_before_adjustment"] = score_before_adjustment
    calc["score"] = adjusted
    return adjusted, calc


def rank_scores(scores: dict[str, Optional[float]]) -> dict[str, int]:
    """按分数从高到低给出竞赛名次（同分同名次）。"""

    ordered = sorted(((site, score) for site, score in scores.items() if score is not None),
                     key=lambda pair: (-pair[1], pair[0]))
    ranks: dict[str, int] = {}
    previous_score: Optional[float] = None
    rank = 0
    for index, (site, score) in enumerate(ordered, start=1):
        if previous_score is None or score != previous_score:
            rank = index
            previous_score = score
        ranks[site] = rank
    return ranks


def weighted_total(cell_scores: dict[str, Optional[float]],
                   weights: dict[str, float]) -> Optional[float]:
    """加权总分；任一指标缺测则总分缺测，避免长期缺陷被指标组合掩盖。"""

    if not cell_scores or any(score is None for score in cell_scores.values()):
        return None
    total_weight = sum(weights[metric_id] for metric_id in cell_scores)
    if total_weight <= 0:
        return None
    total = sum(cell_scores[metric_id] * weights[metric_id] for metric_id in cell_scores)  # type: ignore[operator]
    return round(total / total_weight, 4)


def totals_for_sites(site_metric_scores: dict[str, dict[str, Optional[float]]],
                     weights: dict[str, float]) -> dict[str, Optional[float]]:
    """为每个工厂计算加权总分。"""

    return {site: weighted_total(scores, weights) for site, scores in site_metric_scores.items()}
