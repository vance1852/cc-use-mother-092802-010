"""跨工厂质量对标领域服务。

在基础服务（主体/场所/操作者/审计链）之上提供：
- 指标定义版本化（适用产品、采样窗口、检测方法换版、可比性调整）；
- 对标周期：证据填报 → 异常值独立审查 → 冻结 → 发布；
- 异议只暂停争议指标，其他指标照常发布；
- 发现 → 整改计划 → 复验证据闭环，整改不改写已发布排名；
- 排名用途与产品批次放行权限严格分离；
- 从任一分数回溯原始批次、指标版本与计算规则。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Optional

from .audit import append_event, canonical_json, digest
from .benchmark_models import (
    INDEPENDENT_REVIEW_ROLES,
    RELEASE_ROLES,
    Evidence,
    ExclusionRequest,
    MetricDefinition,
    Publication,
    RankImpact,
)
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .scoring import rank_scores, score_cell, totals_for_sites, validate_rule
from .service import DomainService

METRIC_STATUSES = frozenset({"draft", "active", "retired"})
CYCLE_STATUSES = frozenset({"open", "frozen", "published", "closed"})
FINDING_SOURCES = frozenset({"ranking", "exclusion", "dispute", "audit"})
SEVERITIES = frozenset({"low", "medium", "high"})


class BenchmarkService(DomainService):
    """协调指标版本、周期冻结、发布、异议、整改与批次放行。"""

    # ------------------------------------------------------------------ 工具

    def _idempotent_dict(self, connection, *, request_id: str, action: str,
                         payload: dict[str, Any], create) -> dict[str, Any]:
        """幂等写入并返回完整响应体（重放时从回执读取原始响应）。"""

        receipt = self._idempotent(connection, request_id=request_id, action=action,
                                   payload=payload, create=create)
        row = connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        response = json.loads(row["response_json"])
        return {"request_id": receipt.request_id, "resource_type": receipt.resource_type,
                "resource_id": receipt.resource_id, "replayed": receipt.replayed, **response}

    def _date(self, value: str, field: str) -> str:
        value = str(value).strip()
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc
        return value

    def _iso(self, value: str, field: str) -> str:
        value = self._text(value, field, 40)
        try:
            date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 时间") from exc
        return value

    def _site_org(self, connection, site_id: str) -> str:
        row = connection.execute("SELECT organization_id FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row["organization_id"]

    def _require_site_org(self, connection, actor, site_id: str) -> None:
        organization_id = self._site_org(connection, site_id)
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能操作其他组织场所的数据")

    def _cycle(self, connection, cycle_id: str):
        row = connection.execute("SELECT * FROM benchmark_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise NotFoundError("对标周期不存在")
        return row

    def _metric_row(self, connection, metric_id: str, version: int):
        row = connection.execute(
            "SELECT * FROM metric_definitions WHERE metric_id=? AND version=?", (metric_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("指标定义版本不存在")
        return row

    def _latest_version(self, connection, metric_id: str) -> int:
        row = connection.execute(
            "SELECT MAX(version) AS version FROM metric_definitions WHERE metric_id=?", (metric_id,)
        ).fetchone()
        if row["version"] is None:
            raise NotFoundError("指标不存在")
        return int(row["version"])

    @staticmethod
    def _metric_from_row(row) -> MetricDefinition:
        return MetricDefinition(
            row["metric_id"], int(row["version"]), row["code"], row["name"], row["direction"],
            float(row["weight"]), tuple(json.loads(row["applicable_products_json"])),
            json.loads(row["sampling_json"]), row["method_code"], row["method_version"],
            json.loads(row["rule_json"]), json.loads(row["adjustment_json"]),
            row["status"], row["created_by"], row["created_at"],
        )

    @staticmethod
    def _evidence_from_row(row) -> Evidence:
        return Evidence(
            row["evidence_id"], row["cycle_id"], row["site_id"], row["metric_id"], row["batch_no"],
            row["product_code"], row["method_version"], row["sampled_at"], float(row["raw_value"]),
            json.loads(row["payload_json"]), row["evidence_hash"], bool(row["excluded"]),
            row["submitted_by"], row["created_at"],
        )

    def _pinned_definition(self, connection, cycle_id: str, metric_id: str) -> MetricDefinition:
        pin = connection.execute(
            "SELECT metric_version FROM cycle_metrics WHERE cycle_id=? AND metric_id=?",
            (cycle_id, metric_id),
        ).fetchone()
        if pin is None:
            raise ValidationError("该指标不在本周期内")
        return self._metric_from_row(self._metric_row(connection, metric_id, int(pin["metric_version"])))

    def _snapshot(self, connection, cycle_id: str) -> dict[str, Any]:
        """按当前证据/排除/调整状态计算全部工厂与指标的分数和名次。"""

        pins = connection.execute(
            "SELECT m.* FROM cycle_metrics c JOIN metric_definitions m "
            "ON m.metric_id=c.metric_id AND m.version=c.metric_version WHERE c.cycle_id=? ORDER BY c.metric_id",
            (cycle_id,),
        ).fetchall()
        definitions = [self._metric_from_row(row) for row in pins]
        sites = [row["site_id"] for row in connection.execute(
            "SELECT site_id FROM cycle_sites WHERE cycle_id=? ORDER BY site_id", (cycle_id,))]
        adjustments: dict[tuple[str, str], float] = {}
        for row in connection.execute("SELECT * FROM cycle_adjustments WHERE cycle_id=?", (cycle_id,)):
            adjustments[(row["site_id"], row["metric_id"])] = float(row["factor"])

        cells: dict[str, dict[str, Optional[float]]] = {site: {} for site in sites}
        calcs: dict[tuple[str, str], dict[str, Any]] = {}
        evidence_map: dict[tuple[str, str], list[Evidence]] = {}
        for definition in definitions:
            for site in sites:
                rows = connection.execute(
                    "SELECT * FROM metric_evidence WHERE cycle_id=? AND site_id=? AND metric_id=?",
                    (cycle_id, site, definition.metric_id),
                ).fetchall()
                items = [self._evidence_from_row(row) for row in rows]
                evidence_map[(site, definition.metric_id)] = items
                factor = adjustments.get((site, definition.metric_id), 1.0)
                score, calc = score_cell(definition, items, factor)
                cells[site][definition.metric_id] = score
                calcs[(site, definition.metric_id)] = calc

        weights = {definition.metric_id: definition.weight for definition in definitions}
        metric_ranks: dict[str, dict[str, int]] = {}
        for definition in definitions:
            metric_ranks[definition.metric_id] = rank_scores(
                {site: cells[site][definition.metric_id] for site in sites})
        totals = totals_for_sites(cells, weights)
        total_ranks = rank_scores(totals)
        return {"definitions": definitions, "sites": sites, "weights": weights, "cells": cells,
                "calcs": calcs, "evidence": evidence_map, "metric_ranks": metric_ranks,
                "totals": totals, "total_ranks": total_ranks}

    # ---------------------------------------------------------- 指标定义版本

    def define_metric(self, *, request_id: str, actor_id: str, metric_id: str, code: str, name: str,
                      direction: str, weight: float, applicable_products: list[str],
                      sampling: dict[str, Any], method_code: str, method_version: str,
                      rule: dict[str, Any], adjustment: Optional[dict[str, Any]] = None,
                      status: str = "active") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "metric_id": metric_id, "code": code, "name": name,
                   "direction": direction, "weight": weight,
                   "applicable_products": sorted(applicable_products), "sampling": sampling,
                   "method_code": method_code, "method_version": method_version, "rule": rule,
                   "adjustment": adjustment or {}, "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            metric_id = self._identifier(metric_id, "metric_id")
            code = self._text(code, "code", 80)
            name = self._text(name, "name")
            if direction not in ("higher_better", "lower_better"):
                raise ValidationError("direction 必须是 higher_better 或 lower_better")
            if not isinstance(weight, (int, float)) or weight < 0:
                raise ValidationError("weight 必须是非负数")
            if not isinstance(applicable_products, list) or not applicable_products:
                raise ValidationError("applicable_products 必须是非空数组")
            products = sorted({self._identifier(item, "applicable_products") for item in applicable_products})
            if not isinstance(sampling, dict) or not isinstance(rule, dict):
                raise ValidationError("sampling 与 rule 必须是对象")
            method_code = self._text(method_code, "method_code", 80)
            method_version = self._text(method_version, "method_version", 40)
            if status not in METRIC_STATUSES:
                raise ValidationError("status 必须是 draft/active/retired")
            existing = connection.execute(
                "SELECT code, version FROM metric_definitions WHERE metric_id=? ORDER BY version", (metric_id,)
            ).fetchall()
            version = 1
            if existing:
                if existing[0]["code"] != code:
                    raise ConflictError("指标 code 在新版本中必须保持不变")
                version = int(existing[-1]["version"]) + 1
            draft = MetricDefinition(metric_id, version, code, name, direction, float(weight),
                                     tuple(products), sampling, method_code, method_version,
                                     rule, adjustment or {}, status, actor_id, self._now())
            try:
                validate_rule(draft)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO metric_definitions(metric_id,version,code,name,direction,weight,"
                    "applicable_products_json,sampling_json,method_code,method_version,rule_json,"
                    "adjustment_json,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (metric_id, version, code, name, direction, float(weight),
                     canonical_json(products), canonical_json(sampling), method_code, method_version,
                     canonical_json(rule), canonical_json(adjustment or {}), status,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="metric.defined",
                             resource_type="metric_definition", resource_id=f"{metric_id}:v{version}",
                             detail={"metric_id": metric_id, "version": version, "code": code,
                                     "method_version": method_version, "status": status,
                                     "applicable_products": products},
                             occurred_at=self._now())
                return "metric_definition", f"{metric_id}:v{version}", {
                    "metric_id": metric_id, "version": version}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="define_metric", payload=payload, create=create)

    def retire_metric(self, *, request_id: str, actor_id: str, metric_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "metric_id": metric_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            metric_id = self._identifier(metric_id, "metric_id")
            version = self._latest_version(connection, metric_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE metric_definitions SET status='retired' WHERE metric_id=? AND version=?",
                    (metric_id, version),
                )
                append_event(connection, actor_id=actor_id, action="metric.retired",
                             resource_type="metric_definition", resource_id=f"{metric_id}:v{version}",
                             detail={"metric_id": metric_id, "version": version},
                             occurred_at=self._now())
                return "metric_definition", f"{metric_id}:v{version}", {"version": version}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="retire_metric", payload=payload, create=create)

    def get_metric(self, metric_id: str) -> dict[str, Any]:
        connection = self.database.connection
        rows = connection.execute(
            "SELECT * FROM metric_definitions WHERE metric_id=? ORDER BY version", (metric_id,)).fetchall()
        if not rows:
            raise NotFoundError("指标不存在")
        definitions = [self._metric_from_row(row) for row in rows]
        latest = definitions[-1]
        return {**latest.__dict__, "applicable_products": list(latest.applicable_products),
                "all_versions": [
                    {"version": item.version, "method_version": item.method_version,
                     "status": item.status, "created_at": item.created_at} for item in definitions]}

    # -------------------------------------------------------------- 对标周期

    def create_cycle(self, *, request_id: str, actor_id: str, cycle_id: str, code: str,
                     period_start: str, period_end: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "code": code,
                   "period_start": period_start, "period_end": period_end}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            cycle_id = self._identifier(cycle_id, "cycle_id")
            code = self._text(code, "code", 80)
            period_start = self._date(period_start, "period_start")
            period_end = self._date(period_end, "period_end")
            if period_end < period_start:
                raise ValidationError("period_end 不能早于 period_start")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO benchmark_cycles(cycle_id,code,period_start,period_end,status,"
                        "created_by,created_at) VALUES(?,?,?,?,'open',?,?)",
                        (cycle_id, code, period_start, period_end, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("周期编号或代码已经存在") from exc
                append_event(connection, actor_id=actor_id, action="cycle.created",
                             resource_type="benchmark_cycle", resource_id=cycle_id,
                             detail={"code": code, "period_start": period_start, "period_end": period_end},
                             occurred_at=self._now())
                return "benchmark_cycle", cycle_id, {"cycle_id": cycle_id, "status": "open"}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="create_cycle", payload=payload, create=create)

    def add_cycle_metric(self, *, request_id: str, actor_id: str, cycle_id: str,
                         metric_id: str, metric_version: Optional[int] = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "metric_id": metric_id,
                   "metric_version": metric_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            cycle_id = self._identifier(cycle_id, "cycle_id")
            metric_id = self._identifier(metric_id, "metric_id")
            cycle = self._cycle(connection, cycle_id)
            if cycle["status"] != "open":
                raise ConflictError("周期冻结后不能再增减指标")
            if metric_version is None:
                metric_version = self._latest_version(connection, metric_id)
            row = self._metric_row(connection, metric_id, int(metric_version))
            if row["status"] != "active":
                raise ConflictError("只能把 active 版本的指标加入周期")
            pinned = int(metric_version)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO cycle_metrics(cycle_id,metric_id,metric_version,added_at) VALUES(?,?,?,?)",
                        (cycle_id, metric_id, pinned, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("指标已经在周期内或定义不存在") from exc
                append_event(connection, actor_id=actor_id, action="cycle.metric_added",
                             resource_type="benchmark_cycle", resource_id=cycle_id,
                             detail={"metric_id": metric_id, "metric_version": pinned},
                             occurred_at=self._now())
                return "cycle_metric", f"{cycle_id}:{metric_id}", {
                    "cycle_id": cycle_id, "metric_id": metric_id, "metric_version": pinned}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="add_cycle_metric", payload=payload, create=create)

    def add_cycle_site(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            self._site_org(connection, site_id)
            cycle = self._cycle(connection, self._identifier(cycle_id, "cycle_id"))
            if cycle["status"] != "open":
                raise ConflictError("周期冻结后不能再增减工厂")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO cycle_sites(cycle_id,site_id,joined_at) VALUES(?,?,?)",
                        (cycle_id, site_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("工厂已经加入周期") from exc
                append_event(connection, actor_id=actor_id, action="cycle.site_added",
                             resource_type="benchmark_cycle", resource_id=cycle_id,
                             detail={"site_id": site_id}, occurred_at=self._now())
                return "cycle_site", f"{cycle_id}:{site_id}", {"site_id": site_id}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="add_cycle_site", payload=payload, create=create)

    def set_adjustment(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str,
                       metric_id: str, factor: float, reason: str,
                       evidence: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """登记可比性调整系数（产线改造、检测条件差异等），冻结前生效。"""

        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id,
                   "metric_id": metric_id, "factor": factor, "reason": reason, "evidence": evidence or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            cycle = self._cycle(connection, cycle_id)
            if cycle["status"] != "open":
                raise ConflictError("周期冻结后调整系数不可修改")
            self._pinned_definition(connection, cycle_id, metric_id)
            if not connection.execute(
                    "SELECT 1 FROM cycle_sites WHERE cycle_id=? AND site_id=?", (cycle_id, site_id)).fetchone():
                raise NotFoundError("工厂未加入周期")
            if not isinstance(factor, (int, float)) or factor <= 0:
                raise ValidationError("factor 必须是正数")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO cycle_adjustments(cycle_id,site_id,metric_id,factor,reason,evidence_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(cycle_id,site_id,metric_id) DO UPDATE SET factor=?,reason=?,evidence_json=?",
                    (cycle_id, site_id, metric_id, float(factor), reason,
                     canonical_json(evidence or {}), actor_id, self._now(),
                     float(factor), reason, canonical_json(evidence or {})),
                )
                append_event(connection, actor_id=actor_id, action="adjustment.set",
                             resource_type="cycle_adjustment",
                             resource_id=f"{cycle_id}:{site_id}:{metric_id}",
                             detail={"factor": float(factor), "reason": reason},
                             occurred_at=self._now())
                return "cycle_adjustment", f"{cycle_id}:{site_id}:{metric_id}", {"factor": float(factor)}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="set_adjustment", payload=payload, create=create)

    # ------------------------------------------------------------- 证据填报

    def submit_evidence(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str,
                        metric_id: str, batch_no: str, product_code: str, sampled_at: str,
                        raw_value: float, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        payload_body = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id,
                        "metric_id": metric_id, "batch_no": batch_no, "product_code": product_code,
                        "sampled_at": sampled_at, "raw_value": raw_value, "payload": payload or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._require_site_org(connection, actor, site_id)
            cycle = self._cycle(connection, cycle_id)
            if cycle["status"] != "open":
                raise ConflictError("周期冻结后不能提交证据")
            definition = self._pinned_definition(connection, cycle_id, metric_id)
            if not connection.execute(
                    "SELECT 1 FROM cycle_sites WHERE cycle_id=? AND site_id=?", (cycle_id, site_id)).fetchone():
                raise NotFoundError("工厂未加入周期")
            batch_no = self._identifier(batch_no, "batch_no")
            product_code = self._identifier(product_code, "product_code")
            if product_code not in definition.applicable_products:
                raise ValidationError("该指标不适用于此产品；适用范围以指标版本为准")
            sampled_at = self._iso(sampled_at, "sampled_at")
            if sampled_at[:10] < cycle["period_start"] or sampled_at[:10] > cycle["period_end"]:
                raise ValidationError("采样时间不在周期采样窗口内")
            if not isinstance(raw_value, (int, float)):
                raise ValidationError("raw_value 必须是数值")
            # 检测方法版本必须与周期锁定的指标定义一致（缺省取指标版本）
            evidence_payload = dict(payload or {})
            method_version = evidence_payload.get("method_version") or definition.method_version
            if method_version != definition.method_version:
                raise ValidationError("检测方法版本与周期锁定的指标定义不一致")
            evidence_payload["method_version"] = method_version
            evidence_hash = digest({"batch_no": batch_no, "product_code": product_code,
                                    "sampled_at": sampled_at, "raw_value": raw_value,
                                    "payload": evidence_payload, "method_version": method_version})

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO metric_evidence(evidence_id,cycle_id,site_id,metric_id,batch_no,"
                        "product_code,method_version,sampled_at,raw_value,payload_json,evidence_hash,"
                        "submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (evidence_id, cycle_id, site_id, metric_id, batch_no, product_code,
                         method_version, sampled_at, float(raw_value), canonical_json(evidence_payload),
                         evidence_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该批次证据已经提交") from exc
                append_event(connection, actor_id=actor_id, action="evidence.submitted",
                             resource_type="metric_evidence", resource_id=evidence_id,
                             detail={"cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id,
                                     "batch_no": batch_no, "evidence_hash": evidence_hash},
                             occurred_at=self._now())
                return "metric_evidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="submit_evidence", payload=payload_body, create=create)

    def list_evidence(self, cycle_id: str, site_id: str, metric_id: Optional[str] = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        query = "SELECT * FROM metric_evidence WHERE cycle_id=? AND site_id=?"
        parameters: list[Any] = [cycle_id, site_id]
        if metric_id:
            query += " AND metric_id=?"
            parameters.append(metric_id)
        query += " ORDER BY metric_id, sampled_at, batch_no"
        return [{**self._evidence_from_row(row).__dict__, "payload": json.loads(row["payload_json"])}
                for row in connection.execute(query, parameters)]

    # ------------------------------------------------------- 异常值排除审查

    def request_exclusion(self, *, request_id: str, actor_id: str, cycle_id: str,
                          evidence_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "evidence_id": evidence_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            cycle = self._cycle(connection, cycle_id)
            if cycle["status"] != "open":
                raise ConflictError("异常值排除只能在冻结前申请")
            evidence_row = connection.execute(
                "SELECT * FROM metric_evidence WHERE evidence_id=? AND cycle_id=?",
                (evidence_id, cycle_id)).fetchone()
            if evidence_row is None:
                raise NotFoundError("证据不存在")
            self._require_site_org(connection, actor, evidence_row["site_id"])
            reason = self._text(reason, "reason", 500)
            if connection.execute(
                    "SELECT 1 FROM exclusion_requests WHERE evidence_id=? AND status='pending'",
                    (evidence_id,)).fetchone():
                raise ConflictError("该证据已有待审排除申请")

            def create() -> tuple[str, str, dict[str, Any]]:
                exclusion_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO exclusion_requests(exclusion_id,cycle_id,site_id,metric_id,evidence_id,"
                    "reason,status,requested_by,requested_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                    (exclusion_id, cycle_id, evidence_row["site_id"], evidence_row["metric_id"],
                     evidence_id, reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="exclusion.requested",
                             resource_type="exclusion_request", resource_id=exclusion_id,
                             detail={"cycle_id": cycle_id, "evidence_id": evidence_id,
                                     "site_id": evidence_row["site_id"], "metric_id": evidence_row["metric_id"]},
                             occurred_at=self._now())
                return "exclusion_request", exclusion_id, {"exclusion_id": exclusion_id, "status": "pending"}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="request_exclusion", payload=payload, create=create)

    def review_exclusion(self, *, request_id: str, actor_id: str, exclusion_id: str,
                         decision: str, review_note: str = "") -> dict[str, Any]:
        """独立人员审查排除申请；批准时留存排除前后的全部名次影响。"""

        payload = {"actor_id": actor_id, "exclusion_id": exclusion_id, "decision": decision,
                   "review_note": review_note}
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision 必须是 approved 或 rejected")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *INDEPENDENT_REVIEW_ROLES)
            request_row = connection.execute(
                "SELECT * FROM exclusion_requests WHERE exclusion_id=?", (exclusion_id,)).fetchone()
            if request_row is None:
                raise NotFoundError("排除申请不存在")
            if request_row["status"] != "pending":
                raise ConflictError("排除申请已经审查")
            cycle = self._cycle(connection, request_row["cycle_id"])
            if cycle["status"] != "open":
                raise ConflictError("周期已冻结，不能再改变排除状态")
            # 独立性：审查人不得来自证据所属工厂的组织
            site_org = self._site_org(connection, request_row["site_id"])
            if actor.organization_id == site_org:
                raise PermissionDenied("排除申请必须由工厂以外的独立人员审查")

            target_metric = request_row["metric_id"]
            target_site = request_row["site_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                impact_summary: list[dict[str, Any]] = []
                if decision == "approved":
                    before = self._snapshot(connection, request_row["cycle_id"])
                    connection.execute(
                        "UPDATE metric_evidence SET excluded=1 WHERE evidence_id=?",
                        (request_row["evidence_id"],))
                    after = self._snapshot(connection, request_row["cycle_id"])
                    for scope, ranks_before, scores_before, ranks_after, scores_after in (
                        ("metric", before["metric_ranks"][target_metric],
                         {s: before["cells"][s][target_metric] for s in before["sites"]},
                         after["metric_ranks"][target_metric],
                         {s: after["cells"][s][target_metric] for s in after["sites"]}),
                        ("total", before["total_ranks"], before["totals"],
                         after["total_ranks"], after["totals"]),
                    ):
                        for site in before["sites"]:
                            impact_id = uuid.uuid4().hex
                            connection.execute(
                                "INSERT INTO exclusion_rank_impacts(impact_id,exclusion_id,scope,site_id,"
                                "rank_before,score_before,rank_after,score_after,captured_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?)",
                                (impact_id, exclusion_id, scope, site,
                                 ranks_before.get(site), scores_before.get(site),
                                 ranks_after.get(site), scores_after.get(site), self._now()),
                            )
                            if site == target_site:
                                impact_summary.append({"scope": scope,
                                                       "rank_before": ranks_before.get(site),
                                                       "rank_after": ranks_after.get(site)})
                connection.execute(
                    "UPDATE exclusion_requests SET status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                    "WHERE exclusion_id=?",
                    (decision, actor_id, self._now(), review_note, exclusion_id),
                )
                append_event(connection, actor_id=actor_id, action=f"exclusion.{decision}",
                             resource_type="exclusion_request", resource_id=exclusion_id,
                             detail={"decision": decision, "evidence_id": request_row["evidence_id"],
                                     "impact": impact_summary},
                             occurred_at=self._now())
                return "exclusion_request", exclusion_id, {"exclusion_id": exclusion_id,
                                                           "status": decision, "impact": impact_summary}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="review_exclusion", payload=payload, create=create)

    def get_exclusion(self, exclusion_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM exclusion_requests WHERE exclusion_id=?",
                                 (exclusion_id,)).fetchone()
        if row is None:
            raise NotFoundError("排除申请不存在")
        impacts = [RankImpact(r["scope"], r["site_id"], r["rank_before"], r["score_before"],
                              r["rank_after"], r["score_after"]).__dict__
                   for r in connection.execute(
                       "SELECT * FROM exclusion_rank_impacts WHERE exclusion_id=? ORDER BY scope, site_id",
                       (exclusion_id,))]
        return {**ExclusionRequest(row["exclusion_id"], row["cycle_id"], row["site_id"], row["metric_id"],
                                   row["evidence_id"], row["reason"], row["status"], row["requested_by"],
                                   row["requested_at"], row["reviewed_by"], row["reviewed_at"],
                                   row["review_note"]).__dict__, "rank_impacts": impacts}

    # ----------------------------------------------------------------- 冻结

    def freeze_cycle(self, *, request_id: str, actor_id: str, cycle_id: str) -> dict[str, Any]:
        """冻结周期：固定证据、排除状态、指标版本、调整系数与计算结果。"""

        payload = {"actor_id": actor_id, "cycle_id": cycle_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            cycle = self._cycle(connection, cycle_id)
            if cycle["status"] != "open":
                raise ConflictError("只有 open 周期可以冻结")
            pending = connection.execute(
                "SELECT COUNT(*) AS count FROM exclusion_requests WHERE cycle_id=? AND status='pending'",
                (cycle_id,)).fetchone()["count"]
            if pending:
                raise ConflictError("仍有异常值排除申请未完成独立审查")

            def create() -> tuple[str, str, dict[str, Any]]:
                snapshot = self._snapshot(connection, cycle_id)
                frozen_at = self._now()
                for site in snapshot["sites"]:
                    for definition in snapshot["definitions"]:
                        metric_id = definition.metric_id
                        calc = snapshot["calcs"][(site, metric_id)]
                        factor = float(calc["adjustment_factor"])
                        connection.execute(
                            "INSERT INTO frozen_scores(cycle_id,site_id,metric_id,metric_version,score,"
                            "adjustment_factor,calc_json,rank_metric,frozen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (cycle_id, site, metric_id, definition.version,
                             snapshot["cells"][site][metric_id], factor,
                             canonical_json(calc), snapshot["metric_ranks"][metric_id].get(site), frozen_at),
                        )
                connection.execute(
                    "UPDATE benchmark_cycles SET status='frozen', frozen_at=? WHERE cycle_id=?",
                    (frozen_at, cycle_id),
                )
                append_event(connection, actor_id=actor_id, action="cycle.frozen",
                             resource_type="benchmark_cycle", resource_id=cycle_id,
                             detail={"sites": len(snapshot["sites"]),
                                     "metrics": len(snapshot["definitions"]),
                                     "snapshot_hash": digest(
                                         {s: {m: snapshot["cells"][s][m]
                                              for m in snapshot["weights"]} for s in snapshot["sites"]})},
                             occurred_at=frozen_at)
                return "benchmark_cycle", cycle_id, {
                    "cycle_id": cycle_id, "status": "frozen", "frozen_at": frozen_at}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="freeze_cycle", payload=payload, create=create)

    def frozen_ranking(self, cycle_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._cycle(connection, cycle_id)
        rows = connection.execute(
            "SELECT * FROM frozen_scores WHERE cycle_id=? ORDER BY site_id, metric_id", (cycle_id,)).fetchall()
        if not rows:
            raise NotFoundError("周期尚未冻结")
        cells: dict[str, dict[str, Optional[float]]] = {}
        ranks: dict[str, dict[str, int]] = {}
        versions: dict[str, int] = {}
        for row in rows:
            cells.setdefault(row["site_id"], {})[row["metric_id"]] = row["score"]
            ranks.setdefault(row["metric_id"], {})[row["site_id"]] = row["rank_metric"]
            versions[row["metric_id"]] = row["metric_version"]
        weights = {m: d.weight for m, d in self._definitions_map(connection, cycle_id).items()}
        totals = totals_for_sites(cells, weights)
        return {"cycle_id": cycle_id, "metric_versions": versions, "cells": cells,
                "metric_ranks": ranks, "totals": totals, "total_ranks": rank_scores(totals)}

    def _definitions_map(self, connection, cycle_id: str) -> dict[str, MetricDefinition]:
        rows = connection.execute(
            "SELECT m.* FROM cycle_metrics c JOIN metric_definitions m "
            "ON m.metric_id=c.metric_id AND m.version=c.metric_version WHERE c.cycle_id=?",
            (cycle_id,)).fetchall()
        return {row["metric_id"]: self._metric_from_row(row) for row in rows}

    # ----------------------------------------------------------------- 异议

    def raise_dispute(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str,
                      metric_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id,
                   "metric_id": metric_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            self._require_site_org(connection, actor, site_id)
            cycle = self._cycle(connection, cycle_id)
            if cycle["status"] not in ("frozen", "published", "closed"):
                raise ConflictError("周期冻结后才能提出异议")
            self._pinned_definition(connection, cycle_id, metric_id)
            reason = self._text(reason, "reason", 500)
            if connection.execute(
                    "SELECT 1 FROM metric_disputes WHERE cycle_id=? AND site_id=? AND metric_id=?",
                    (cycle_id, site_id, metric_id)).fetchone():
                raise ConflictError("该指标异议已经存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                dispute_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO metric_disputes(dispute_id,cycle_id,site_id,metric_id,reason,status,"
                    "raised_by,raised_at) VALUES(?,?,?,?,?,'open',?,?)",
                    (dispute_id, cycle_id, site_id, metric_id, reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="dispute.raised",
                             resource_type="metric_dispute", resource_id=dispute_id,
                             detail={"cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id},
                             occurred_at=self._now())
                return "metric_dispute", dispute_id, {"dispute_id": dispute_id, "status": "open"}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="raise_dispute", payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str, resolution: str,
                        corrected_score: Optional[float] = None, note: str = "") -> dict[str, Any]:
        """裁定异议；corrected 时登记更正分，原冻结分保留，需重新发布才生效。"""

        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "resolution": resolution,
                   "corrected_score": corrected_score, "note": note}
        if resolution not in ("upheld", "corrected"):
            raise ValidationError("resolution 必须是 upheld 或 corrected")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            row = connection.execute("SELECT * FROM metric_disputes WHERE dispute_id=?",
                                     (dispute_id,)).fetchone()
            if row is None:
                raise NotFoundError("异议不存在")
            if row["status"] != "open":
                raise ConflictError("异议已经裁定")
            if resolution == "corrected":
                if not isinstance(corrected_score, (int, float)) or not 0 <= float(corrected_score) <= 100:
                    raise ValidationError("corrected_score 必须是 0 到 100 的数值")
            frozen = connection.execute(
                "SELECT score FROM frozen_scores WHERE cycle_id=? AND site_id=? AND metric_id=?",
                (row["cycle_id"], row["site_id"], row["metric_id"])).fetchone()
            if frozen is None:
                raise NotFoundError("冻结分数不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                new_status = "resolved_upheld" if resolution == "upheld" else "resolved_corrected"
                connection.execute(
                    "UPDATE metric_disputes SET status=?, resolved_by=?, resolved_at=?, resolution_note=? "
                    "WHERE dispute_id=?",
                    (new_status, actor_id, self._now(), note, dispute_id),
                )
                correction_id = None
                if resolution == "corrected":
                    correction_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO score_corrections(correction_id,dispute_id,cycle_id,site_id,metric_id,"
                        "original_score,corrected_score,reason,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (correction_id, dispute_id, row["cycle_id"], row["site_id"], row["metric_id"],
                         frozen["score"], float(corrected_score), note, actor_id, self._now()),
                    )
                append_event(connection, actor_id=actor_id, action=f"dispute.resolved_{resolution}",
                             resource_type="metric_dispute", resource_id=dispute_id,
                             detail={"resolution": resolution,
                                     "original_score": frozen["score"],
                                     "corrected_score": corrected_score if resolution == "corrected" else None,
                                     "correction_id": correction_id},
                             occurred_at=self._now())
                return "metric_dispute", dispute_id, {"dispute_id": dispute_id, "status": new_status,
                                                      "correction_id": correction_id}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="resolve_dispute", payload=payload, create=create)

    def withdraw_dispute(self, *, request_id: str, actor_id: str, dispute_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute("SELECT * FROM metric_disputes WHERE dispute_id=?",
                                     (dispute_id,)).fetchone()
            if row is None:
                raise NotFoundError("异议不存在")
            if row["status"] != "open":
                raise ConflictError("已裁定的异议不能撤回")
            if actor.role != "admin" and actor.actor_id != row["raised_by"]:
                raise PermissionDenied("只能由提出者撤回异议")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE metric_disputes SET status='withdrawn' WHERE dispute_id=?", (dispute_id,))
                append_event(connection, actor_id=actor_id, action="dispute.withdrawn",
                             resource_type="metric_dispute", resource_id=dispute_id,
                             detail={}, occurred_at=self._now())
                return "metric_dispute", dispute_id, {"status": "withdrawn"}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="withdraw_dispute", payload=payload, create=create)

    def list_disputes(self, cycle_id: str, status: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM metric_disputes WHERE cycle_id=?"
        parameters: list[Any] = [cycle_id]
        if status:
            query += " AND status=?"
            parameters.append(status)
        return [dict(row) for row in self.database.connection.execute(query + " ORDER BY raised_at",
                                                                       parameters)]

    # ----------------------------------------------------------------- 发布

    def publish_cycle(self, *, request_id: str, actor_id: str, cycle_id: str,
                      note: str = "") -> dict[str, Any]:
        """发布排名快照：未裁定的争议指标暂停，其他指标照常发布。"""

        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            cycle = self._cycle(connection, cycle_id)
            if cycle["status"] not in ("frozen", "published"):
                raise ConflictError("周期冻结后才能发布")
            frozen_rows = connection.execute(
                "SELECT * FROM frozen_scores WHERE cycle_id=?", (cycle_id,)).fetchall()
            if not frozen_rows:
                raise NotFoundError("冻结结果不存在")
            version_row = connection.execute(
                "SELECT MAX(version) AS version FROM cycle_publications WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            version = int(version_row["version"] or 0) + 1

            def create() -> tuple[str, str, dict[str, Any]]:
                published_at = self._now()
                open_disputes = {
                    (r["site_id"], r["metric_id"]) for r in connection.execute(
                        "SELECT site_id, metric_id FROM metric_disputes WHERE cycle_id=? AND status='open'",
                        (cycle_id,))}
                corrections = {
                    (r["site_id"], r["metric_id"]): float(r["corrected_score"]) for r in connection.execute(
                        "SELECT sc.* FROM score_corrections sc JOIN metric_disputes d "
                        "ON sc.dispute_id=d.dispute_id WHERE sc.cycle_id=? AND d.status='resolved_corrected'",
                        (cycle_id,))}
                sites = sorted({r["site_id"] for r in frozen_rows})
                metrics = sorted({r["metric_id"] for r in frozen_rows})

                cells: dict[str, dict[str, Optional[float]]] = {site: {} for site in sites}
                states: dict[tuple[str, str], str] = {}
                versions: dict[str, int] = {}
                for row in frozen_rows:
                    key = (row["site_id"], row["metric_id"])
                    versions[row["metric_id"]] = row["metric_version"]
                    if key in open_disputes:
                        cells[row["site_id"]][row["metric_id"]] = None
                        states[key] = "held_disputed"
                    else:
                        cells[row["site_id"]][row["metric_id"]] = corrections.get(key, row["score"])
                        states[key] = "published"
                metric_ranks = {metric: rank_scores({site: cells[site][metric] for site in sites})
                                for metric in metrics}
                weights = {m: d.weight for m, d in self._definitions_map(connection, cycle_id).items()}
                # 争议指标暂停时，其所在工厂的总分同步暂停；其余工厂照常排名
                publishable_total = {}
                for site in sites:
                    if any(states[(site, metric)] == "held_disputed" for metric in metrics):
                        publishable_total[site] = None
                    else:
                        from .scoring import weighted_total
                        publishable_total[site] = weighted_total(cells[site], weights)
                total_ranks = rank_scores(publishable_total)

                manifest = {
                    "cycle_id": cycle_id, "version": version, "frozen_at": cycle["frozen_at"],
                    "metric_versions": versions, "weights": weights,
                    "held_disputed": [{"site_id": s, "metric_id": m}
                                      for s, m in sorted(open_disputes)],
                    "corrections_applied": [{"site_id": s, "metric_id": m, "corrected_score": v}
                                            for (s, m), v in sorted(corrections.items())],
                    "scores": {site: cells[site] for site in sites},
                    "metric_ranks": metric_ranks, "total_scores": publishable_total,
                    "total_ranks": total_ranks, "note": note,
                }
                manifest_hash = digest(manifest)
                publication_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cycle_publications(publication_id,cycle_id,version,manifest_json,"
                    "manifest_hash,published_by,published_at) VALUES(?,?,?,?,?,?,?)",
                    (publication_id, cycle_id, version, canonical_json(manifest), manifest_hash,
                     actor_id, published_at),
                )
                for site in sites:
                    for metric in metrics:
                        connection.execute(
                            "INSERT INTO published_scores(cycle_id,publication_version,site_id,metric_id,"
                            "metric_version,score,rank_metric,state,total_score,rank_total) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (cycle_id, version, site, metric, versions[metric], cells[site][metric],
                             metric_ranks[metric].get(site), states[(site, metric)],
                             publishable_total[site], total_ranks.get(site)),
                        )
                connection.execute(
                    "UPDATE benchmark_cycles SET status='published', published_at=? WHERE cycle_id=?",
                    (published_at, cycle_id),
                )
                append_event(connection, actor_id=actor_id, action="cycle.published",
                             resource_type="cycle_publication", resource_id=publication_id,
                             detail={"cycle_id": cycle_id, "version": version,
                                     "held": len(open_disputes), "corrections": len(corrections),
                                     "manifest_hash": manifest_hash},
                             occurred_at=published_at)
                return "cycle_publication", publication_id, {
                    "cycle_id": cycle_id, "version": version, "publication_id": publication_id,
                    "manifest_hash": manifest_hash,
                    "held_disputed": [{"site_id": s, "metric_id": m} for s, m in sorted(open_disputes)]}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="publish_cycle", payload=payload, create=create)

    def get_publication(self, cycle_id: str, version: Optional[int] = None) -> dict[str, Any]:
        connection = self.database.connection
        self._cycle(connection, cycle_id)
        if version is None:
            row = connection.execute(
                "SELECT * FROM cycle_publications WHERE cycle_id=? ORDER BY version DESC LIMIT 1",
                (cycle_id,)).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM cycle_publications WHERE cycle_id=? AND version=?", (cycle_id, version)
            ).fetchone()
        if row is None:
            raise NotFoundError("发布版本不存在")
        manifest = json.loads(row["manifest_json"])
        published = [dict(r) for r in connection.execute(
            "SELECT site_id,metric_id,metric_version,score,rank_metric,state,total_score,rank_total "
            "FROM published_scores WHERE cycle_id=? AND publication_version=? "
            "ORDER BY site_id, metric_id", (cycle_id, row["version"]))]
        publication = Publication(row["publication_id"], cycle_id, int(row["version"]), manifest,
                                  row["manifest_hash"], row["published_by"], row["published_at"])
        return {**publication.__dict__, "rows": published}

    # ------------------------------------------------------------- 整改闭环

    def create_finding(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str,
                       source: str, description: str, severity: str,
                       metric_id: Optional[str] = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id, "source": source,
                   "description": description, "severity": severity, "metric_id": metric_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager", "reviewer")
            self._cycle(connection, cycle_id)
            self._site_org(connection, site_id)
            if source not in FINDING_SOURCES:
                raise ValidationError("source 必须是 ranking/exclusion/dispute/audit")
            if severity not in SEVERITIES:
                raise ValidationError("severity 必须是 low/medium/high")
            description = self._text(description, "description", 1000)
            metric_version = None
            if metric_id:
                metric_version = self._pinned_definition(connection, cycle_id, metric_id).version

            def create() -> tuple[str, str, dict[str, Any]]:
                finding_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO findings(finding_id,cycle_id,site_id,metric_id,metric_version,source,"
                    "description,severity,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (finding_id, cycle_id, site_id, metric_id, metric_version, source, description,
                     severity, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="finding.created",
                             resource_type="finding", resource_id=finding_id,
                             detail={"cycle_id": cycle_id, "site_id": site_id, "source": source,
                                     "severity": severity, "metric_id": metric_id},
                             occurred_at=self._now())
                return "finding", finding_id, {"finding_id": finding_id}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="create_finding", payload=payload, create=create)

    def create_rectification_plan(self, *, request_id: str, actor_id: str, finding_id: str,
                                  owner_actor_id: str, due_date: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "finding_id": finding_id,
                   "owner_actor_id": owner_actor_id, "due_date": due_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "quality_manager")
            finding = connection.execute("SELECT * FROM findings WHERE finding_id=?",
                                         (finding_id,)).fetchone()
            if finding is None:
                raise NotFoundError("发现不存在")
            owner = self._actor(connection, owner_actor_id)
            due_date = self._date(due_date, "due_date")
            if due_date < self._now()[:10]:
                raise ValidationError("due_date 不能早于今天")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO rectification_plans(plan_id,finding_id,owner_actor_id,due_date,status,"
                        "created_by,created_at) VALUES(?,?,?,?,'open',?,?)",
                        (plan_id, finding_id, owner_actor_id, due_date, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该发现已经存在整改计划") from exc
                append_event(connection, actor_id=actor_id, action="rectification.plan_created",
                             resource_type="rectification_plan", resource_id=plan_id,
                             detail={"finding_id": finding_id, "owner_actor_id": owner_actor_id,
                                     "due_date": due_date, "site_id": finding["site_id"]},
                             occurred_at=self._now())
                return "rectification_plan", plan_id, {"plan_id": plan_id, "status": "open"}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="create_rectification_plan", payload=payload, create=create)

    def submit_verification(self, *, request_id: str, actor_id: str, plan_id: str,
                            evidence: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(evidence, dict) or not evidence:
            raise ValidationError("复验证据必须是非空对象")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            plan = connection.execute("SELECT * FROM rectification_plans WHERE plan_id=?",
                                      (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("整改计划不存在")
            if actor.role != "admin" and actor.actor_id != plan["owner_actor_id"]:
                raise PermissionDenied("只有责任人可以提交复验证据")
            if plan["status"] == "closed":
                raise ConflictError("计划已关闭")

            def create() -> tuple[str, str, dict[str, Any]]:
                verification_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rectification_verifications(verification_id,plan_id,evidence_json,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?)",
                    (verification_id, plan_id, canonical_json(evidence), actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE rectification_plans SET status='verification_submitted' WHERE plan_id=?",
                    (plan_id,))
                append_event(connection, actor_id=actor_id, action="rectification.verification_submitted",
                             resource_type="rectification_verification", resource_id=verification_id,
                             detail={"plan_id": plan_id}, occurred_at=self._now())
                return "rectification_verification", verification_id, {"verification_id": verification_id}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="submit_verification", payload=payload, create=create)

    def review_verification(self, *, request_id: str, actor_id: str, verification_id: str,
                            accepted: bool, review_note: str = "") -> dict[str, Any]:
        """独立人员复验；通过才关闭计划。关闭计划不会改写任何已发布排名。"""

        payload = {"actor_id": actor_id, "verification_id": verification_id, "accepted": accepted,
                   "review_note": review_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *INDEPENDENT_REVIEW_ROLES)
            verification = connection.execute(
                "SELECT * FROM rectification_verifications WHERE verification_id=?",
                (verification_id,)).fetchone()
            if verification is None:
                raise NotFoundError("复验记录不存在")
            if verification["accepted"] is not None:
                raise ConflictError("复验已经裁定")
            plan = connection.execute("SELECT * FROM rectification_plans WHERE plan_id=?",
                                      (verification["plan_id"],)).fetchone()
            finding = connection.execute("SELECT site_id FROM findings WHERE finding_id=?",
                                         (plan["finding_id"],)).fetchone()
            if actor.organization_id == self._site_org(connection, finding["site_id"]):
                raise PermissionDenied("复验必须由工厂以外的独立人员裁定")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE rectification_verifications SET reviewed_by=?, reviewed_at=?, accepted=?, "
                    "review_note=? WHERE verification_id=?",
                    (actor_id, self._now(), 1 if accepted else 0, review_note, verification_id),
                )
                if accepted:
                    connection.execute(
                        "UPDATE rectification_plans SET status='closed', closed_at=? WHERE plan_id=?",
                        (self._now(), verification["plan_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE rectification_plans SET status='open' WHERE plan_id=?",
                        (verification["plan_id"],))
                append_event(connection, actor_id=actor_id,
                             action="rectification.verified" if accepted else "rectification.verification_rejected",
                             resource_type="rectification_verification", resource_id=verification_id,
                             detail={"plan_id": verification["plan_id"], "accepted": bool(accepted),
                                     "ranking_unchanged": True},
                             occurred_at=self._now())
                return "rectification_verification", verification_id, {
                    "verification_id": verification_id, "accepted": bool(accepted),
                    "plan_status": "closed" if accepted else "open"}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="review_verification", payload=payload, create=create)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = connection.execute("SELECT * FROM rectification_plans WHERE plan_id=?",
                                  (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("整改计划不存在")
        finding = connection.execute("SELECT * FROM findings WHERE finding_id=?",
                                     (plan["finding_id"],)).fetchone()
        verifications = [dict(row) for row in connection.execute(
            "SELECT * FROM rectification_verifications WHERE plan_id=? ORDER BY submitted_at", (plan_id,))]
        return {"plan": dict(plan), "finding": dict(finding), "verifications": verifications}

    def list_findings(self, cycle_id: str, site_id: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM findings WHERE cycle_id=?"
        parameters: list[Any] = [cycle_id]
        if site_id:
            query += " AND site_id=?"
            parameters.append(site_id)
        return [dict(row) for row in self.database.connection.execute(
            query + " ORDER BY created_at", parameters)]

    # --------------------------------------------------------- 批次放行权限

    def decide_batch_release(self, *, request_id: str, actor_id: str, site_id: str, batch_no: str,
                             product_code: str, decision: str, reason: str) -> dict[str, Any]:
        """产品批次放行。与排名角色严格分离：quality_manager/reviewer/operator 无权放行。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "batch_no": batch_no,
                   "product_code": product_code, "decision": decision, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *RELEASE_ROLES)
            self._require_site_org(connection, actor, site_id)
            if decision not in ("released", "rejected", "held"):
                raise ValidationError("decision 必须是 released/rejected/held")
            batch_no = self._identifier(batch_no, "batch_no")
            product_code = self._identifier(product_code, "product_code")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT decision FROM batch_releases WHERE site_id=? AND batch_no=?",
                    (site_id, batch_no)).fetchone()
                if existing:
                    raise ConflictError("批次已经作出放行决定，决定不可改写")
                release_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO batch_releases(release_id,site_id,batch_no,product_code,decision,reason,"
                    "decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                    (release_id, site_id, batch_no, product_code, decision, reason,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="batch_release.decided",
                             resource_type="batch_release", resource_id=release_id,
                             detail={"site_id": site_id, "batch_no": batch_no, "decision": decision},
                             occurred_at=self._now())
                return "batch_release", release_id, {"release_id": release_id, "decision": decision}

            return self._idempotent_dict(connection, request_id=request_id,
                                    action="decide_batch_release", payload=payload, create=create)

    def list_batch_releases(self, site_id: str, batch_no: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM batch_releases WHERE site_id=?"
        parameters: list[Any] = [site_id]
        if batch_no:
            query += " AND batch_no=?"
            parameters.append(batch_no)
        return [dict(row) for row in self.database.connection.execute(
            query + " ORDER BY decided_at", parameters)]

    # ------------------------------------------------- 分数追溯（管理层 API）

    def trace_score(self, cycle_id: str, site_id: str, metric_id: str) -> dict[str, Any]:
        """从任一发布分数回溯：指标版本/规则/调整 → 原始批次 → 冻结计算 → 更正 → 历次发布。"""

        connection = self.database.connection
        self._cycle(connection, cycle_id)
        definition = self._pinned_definition(connection, cycle_id, metric_id)
        frozen = connection.execute(
            "SELECT * FROM frozen_scores WHERE cycle_id=? AND site_id=? AND metric_id=?",
            (cycle_id, site_id, metric_id)).fetchone()
        if frozen is None:
            raise NotFoundError("该工厂指标没有冻结结果")
        adjustment = connection.execute(
            "SELECT * FROM cycle_adjustments WHERE cycle_id=? AND site_id=? AND metric_id=?",
            (cycle_id, site_id, metric_id)).fetchone()
        evidence_rows = connection.execute(
            "SELECT * FROM metric_evidence WHERE cycle_id=? AND site_id=? AND metric_id=? ORDER BY batch_no",
            (cycle_id, site_id, metric_id)).fetchall()
        exclusions = {}
        for row in connection.execute(
                "SELECT * FROM exclusion_requests WHERE cycle_id=? AND site_id=? AND metric_id=?",
                (cycle_id, site_id, metric_id)):
            exclusions[row["evidence_id"]] = {"exclusion_id": row["exclusion_id"], "status": row["status"],
                                              "reviewed_by": row["reviewed_by"]}
        evidence = []
        for row in evidence_rows:
            evidence.append({"evidence_id": row["evidence_id"], "batch_no": row["batch_no"],
                             "product_code": row["product_code"], "method_version": row["method_version"],
                             "sampled_at": row["sampled_at"], "raw_value": row["raw_value"],
                             "evidence_hash": row["evidence_hash"], "excluded": bool(row["excluded"]),
                             "exclusion": exclusions.get(row["evidence_id"])})
        correction = connection.execute(
            "SELECT sc.* FROM score_corrections sc JOIN metric_disputes d ON sc.dispute_id=d.dispute_id "
            "WHERE sc.cycle_id=? AND sc.site_id=? AND sc.metric_id=?",
            (cycle_id, site_id, metric_id)).fetchone()
        dispute = connection.execute(
            "SELECT * FROM metric_disputes WHERE cycle_id=? AND site_id=? AND metric_id=?",
            (cycle_id, site_id, metric_id)).fetchone()
        publications = []
        for row in connection.execute(
                "SELECT p.version,p.published_at,p.manifest_hash,s.score,s.rank_metric,s.state,"
                "s.total_score,s.rank_total FROM published_scores s JOIN cycle_publications p "
                "ON p.cycle_id=s.cycle_id AND p.version=s.publication_version "
                "WHERE s.cycle_id=? AND s.site_id=? AND s.metric_id=? ORDER BY p.version",
                (cycle_id, site_id, metric_id)):
            publications.append(dict(row))
        return {
            "cycle_id": cycle_id, "site_id": site_id,
            "metric_definition": {**definition.__dict__,
                                  "applicable_products": list(definition.applicable_products)},
            "adjustment": None if adjustment is None else {
                "factor": adjustment["factor"], "reason": adjustment["reason"],
                "evidence": json.loads(adjustment["evidence_json"]),
                "created_by": adjustment["created_by"]},
            "evidence": evidence,
            "frozen": {"score": frozen["score"], "metric_version": frozen["metric_version"],
                       "rank_metric": frozen["rank_metric"], "frozen_at": frozen["frozen_at"],
                       "calc": json.loads(frozen["calc_json"])},
            "dispute": None if dispute is None else dict(dispute),
            "correction": None if correction is None else dict(correction),
            "publications": publications,
        }
