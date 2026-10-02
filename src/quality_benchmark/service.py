"""跨工厂质量对标平台的核心领域服务。

在基础服务的主体、场所、角色、幂等与哈希审计能力之上提供：

- 版本化指标定义：适用产品、采样窗口、检测方法换版与可比性调整随版本固化；
- 排名周期：创建时锁定指标版本，冻结时把各工厂证据与计算结果固化为快照；
- 异常值排除：工厂申请、独立人员审查，保留排除前后的名次影响；
- 异议处理：只暂停争议指标的发布，其余指标照常发布；
- 整改闭环：计划关联发现、责任人、期限与复验证据，完成整改不改写已发布排名；
- 追溯与权限分离：任一分数可追溯到原始批次与计算规则，排名用途与批次放行权限互相独立。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from beverage_ops_foundation.audit import append_event, canonical_json, digest
from beverage_ops_foundation.clock import Clock
from beverage_ops_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from beverage_ops_foundation.models import Actor, WriteReceipt
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


DIRECTIONS = frozenset({"higher_better", "lower_better"})
PERMISSIONS = frozenset({"ranking.view", "ranking.trace", "batch.release"})
FINDING_TYPES = frozenset({"metric_result", "dispute", "exclusion"})
DISPUTE_OUTCOMES = frozenset({"upheld", "rejected"})
RELEASE_DECISIONS = frozenset({"released", "rejected"})
ADJUSTMENT_KEYS = frozenset({"product_coefficients", "line_upgrade_factor", "method_offset"})


class QualityService(DomainService):
    """协调质量对标领域的权限、幂等、事务与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)

    # ---------- 校验辅助 ----------

    def _date(self, value: str, field: str) -> str:
        value = str(value).strip()
        if len(value) != 10:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc
        return value

    def _number(self, value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是数值")
        return float(value)

    def _flag(self, value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValidationError(f"{field} 必须是布尔值")
        return value

    def _products(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("applicable_products 必须是非空数组")
        products = [self._identifier(str(item), "applicable_products") for item in value]
        if len(set(products)) != len(products):
            raise ValidationError("applicable_products 存在重复")
        return products

    def _window(self, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValidationError("sampling_window 必须是对象")
        start = self._date(str(value.get("start", "")), "sampling_window.start")
        end = self._date(str(value.get("end", "")), "sampling_window.end")
        if start > end:
            raise ValidationError("采样窗口的起止日期顺序无效")
        return {"start": start, "end": end}

    def _adjustment(self, value: Any) -> dict[str, Any]:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValidationError("adjustment 必须是对象")
        unknown = sorted(set(value) - ADJUSTMENT_KEYS)
        if unknown:
            raise ValidationError(f"adjustment 包含未知字段: {unknown}")
        raw_coefficients = value.get("product_coefficients", {})
        if not isinstance(raw_coefficients, dict):
            raise ValidationError("product_coefficients 必须是对象")
        coefficients = {}
        for product, coefficient in raw_coefficients.items():
            coefficient = self._number(coefficient, "product_coefficients")
            if coefficient <= 0:
                raise ValidationError("产品可比性系数必须大于 0")
            coefficients[self._identifier(str(product), "product_coefficients")] = coefficient
        line_factor = self._number(value.get("line_upgrade_factor", 1.0), "line_upgrade_factor")
        if line_factor <= 0:
            raise ValidationError("产线改造系数必须大于 0")
        offset = self._number(value.get("method_offset", 0.0), "method_offset")
        return {"product_coefficients": coefficients,
                "line_upgrade_factor": line_factor,
                "method_offset": offset}

    def _measurements(self, value: Any, products: list[str], window: dict[str, str]) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValidationError("measurements 必须是非空数组")
        if len(value) > 1000:
            raise ValidationError("measurements 超出单次提交上限")
        measurements = []
        seen_batches = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("measurements 元素必须是对象")
            batch_id = self._identifier(str(item.get("batch_id", "")), "batch_id")
            if batch_id in seen_batches:
                raise ValidationError("同一提交内批次号重复")
            seen_batches.add(batch_id)
            product = str(item.get("product", ""))
            if product not in products:
                raise ValidationError(f"产品 {product} 不在指标版本的适用产品范围内")
            number = self._number(item.get("value"), "value")
            sampled_at = self._date(str(item.get("sampled_at", "")), "sampled_at")
            if not window["start"] <= sampled_at <= window["end"]:
                raise ValidationError("采样日期不在指标版本的采样窗口内")
            measurements.append({"batch_id": batch_id, "product": product,
                                 "value": number, "sampled_at": sampled_at})
        return measurements

    # ---------- 权限辅助 ----------

    def _has_permission(self, connection, actor: Actor, permission: str) -> bool:
        if actor.role == "admin":
            return True
        row = connection.execute(
            "SELECT 1 FROM permission_grants WHERE actor_id=? AND permission=?",
            (actor.actor_id, permission),
        ).fetchone()
        return row is not None

    def _require_permission(self, connection, actor: Actor, permission: str) -> None:
        if not self._has_permission(connection, actor, permission):
            raise PermissionDenied(f"缺少权限 {permission}")

    def _require_site_org(self, actor: Actor, site) -> None:
        if actor.role == "admin":
            return
        if actor.organization_id != site["organization_id"]:
            raise PermissionDenied("只能操作本组织工厂的数据")

    def _require_independent_reviewer(self, actor: Actor, organization_id: str,
                                      requester_id: str | None = None) -> None:
        if actor.role == "admin":
            return
        if actor.role != "reviewer":
            raise PermissionDenied("需要独立审查人执行该动作")
        if actor.organization_id == organization_id:
            raise PermissionDenied("审查人必须独立于被审查工厂所属组织")
        if requester_id is not None and actor.actor_id == requester_id:
            raise PermissionDenied("审查人不能是申请人本人")

    # ---------- 数据读取辅助 ----------

    def _metric_version_row(self, connection, metric_version_id: str):
        row = connection.execute(
            "SELECT * FROM metric_versions WHERE metric_version_id=?", (metric_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("指标版本不存在")
        return row

    def _cycle_row(self, connection, cycle_id: str):
        row = connection.execute(
            "SELECT * FROM ranking_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("排名周期不存在")
        return row

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _snapshot_row(self, connection, cycle_id: str, site_id: str, metric_id: str):
        row = connection.execute(
            "SELECT * FROM score_snapshots WHERE cycle_id=? AND site_id=? AND metric_id=?",
            (cycle_id, site_id, metric_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("该工厂在周期内没有此指标的冻结分数")
        return row

    def _pinned_version(self, connection, cycle_id: str, metric_id: str):
        return connection.execute(
            "SELECT mv.* FROM cycle_metrics cm "
            "JOIN metric_versions mv ON mv.metric_version_id=cm.metric_version_id "
            "WHERE cm.cycle_id=? AND cm.metric_id=?",
            (cycle_id, metric_id),
        ).fetchone()

    # ---------- 计算与排名 ----------

    def _compute_scores(self, metric_version, measurements: list[dict[str, Any]],
                        excluded_batches: set[str] | None = None) -> tuple[float, float, dict[str, Any]]:
        adjustment = json.loads(metric_version["adjustment_json"])
        coefficients = adjustment["product_coefficients"]
        line_factor = adjustment["line_upgrade_factor"]
        offset = adjustment["method_offset"]
        excluded_batches = excluded_batches or set()
        active = [item for item in measurements if item["batch_id"] not in excluded_batches]
        raw = round(sum(item["value"] for item in active) / len(active), 6)
        mix = round(sum(coefficients.get(item["product"], 1.0) for item in active) / len(active), 6)
        adjusted = round(raw * mix * line_factor + offset, 6)
        computation = {
            "metric_version_id": metric_version["metric_version_id"],
            "method_version": metric_version["method_version"],
            "formula": "adjusted = mean(values) * mean(product_coefficients) "
                       "* line_upgrade_factor + method_offset",
            "measurement_count": len(active),
            "measurements_hash": digest(active),
            "batches": sorted(item["batch_id"] for item in active),
            "excluded_batches": sorted(excluded_batches),
            "product_mix_coefficient": mix,
            "line_upgrade_factor": line_factor,
            "method_offset": offset,
        }
        return raw, adjusted, computation

    def _metric_ranking(self, connection, cycle_id: str, metric_id: str) -> dict[str, dict[str, Any]]:
        pinned = self._pinned_version(connection, cycle_id, metric_id)
        rows = connection.execute(
            "SELECT site_id, adjusted_score FROM score_snapshots "
            "WHERE cycle_id=? AND metric_id=? AND excluded=0 AND suspended=0",
            (cycle_id, metric_id),
        ).fetchall()
        higher_better = pinned["direction"] == "higher_better"
        ordered = sorted(
            rows,
            key=lambda row: (-row["adjusted_score"] if higher_better else row["adjusted_score"], row["site_id"]),
        )
        ranking: dict[str, dict[str, Any]] = {}
        last_score: float | None = None
        last_rank = 0
        for index, row in enumerate(ordered, start=1):
            if last_score is not None and row["adjusted_score"] == last_score:
                rank = last_rank
            else:
                rank = index
            ranking[row["site_id"]] = {"adjusted_score": row["adjusted_score"], "rank": rank}
            last_score = row["adjusted_score"]
            last_rank = rank
        return ranking

    def _cycle_totals(self, connection, cycle_id: str,
                      metric_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        query = ("SELECT cm.metric_id, mv.direction, mv.weight FROM cycle_metrics cm "
                 "JOIN metric_versions mv ON mv.metric_version_id=cm.metric_version_id "
                 "WHERE cm.cycle_id=?")
        parameters: list[Any] = [cycle_id]
        if metric_ids is not None:
            if not metric_ids:
                return {}
            placeholders = ",".join("?" for _ in metric_ids)
            query += f" AND cm.metric_id IN ({placeholders})"
            parameters.extend(metric_ids)
        metrics = connection.execute(query, parameters).fetchall()
        if not metrics:
            return {}
        snapshots = connection.execute(
            "SELECT metric_id, site_id, adjusted_score FROM score_snapshots "
            "WHERE cycle_id=? AND excluded=0 AND suspended=0",
            (cycle_id,),
        ).fetchall()
        by_metric: dict[str, dict[str, float]] = {}
        for row in snapshots:
            by_metric.setdefault(row["metric_id"], {})[row["site_id"]] = row["adjusted_score"]
        normalized: dict[tuple[str, str], float] = {}
        for metric in metrics:
            scores = by_metric.get(metric["metric_id"], {})
            if not scores:
                continue
            low = min(scores.values())
            high = max(scores.values())
            for site_id, score in scores.items():
                if high == low:
                    value = 100.0
                elif metric["direction"] == "higher_better":
                    value = (score - low) / (high - low) * 100.0
                else:
                    value = (high - score) / (high - low) * 100.0
                normalized[(metric["metric_id"], site_id)] = value
        weight_sum = sum(metric["weight"] for metric in metrics)
        sites = sorted({site for metric in metrics for site in by_metric.get(metric["metric_id"], {})})
        totals = {}
        for site_id in sites:
            total = sum(
                metric["weight"] * normalized.get((metric["metric_id"], site_id), 0.0)
                for metric in metrics
            ) / weight_sum
            totals[site_id] = round(total, 6)
        ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        result: dict[str, dict[str, Any]] = {}
        last_score: float | None = None
        last_rank = 0
        for index, (site_id, total) in enumerate(ordered, start=1):
            if last_score is not None and total == last_score:
                rank = last_rank
            else:
                rank = index
            result[site_id] = {"total_score": total, "rank": rank}
            last_score = total
            last_rank = rank
        return result

    # ---------- 指标定义与换版 ----------

    def define_metric_version(self, *, request_id: str, actor_id: str, metric_id: str, name: str,
                              unit: str, direction: str, weight: float,
                              applicable_products: list[str], sampling_window: dict[str, Any],
                              method_version: str,
                              adjustment: dict[str, Any] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "metric_id": metric_id, "name": name, "unit": unit,
                   "direction": direction, "weight": weight, "applicable_products": applicable_products,
                   "sampling_window": sampling_window, "method_version": method_version,
                   "adjustment": adjustment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            metric_id = self._identifier(metric_id, "metric_id")
            name = self._text(name, "name")
            unit = self._text(unit, "unit", 40)
            if direction not in DIRECTIONS:
                raise ValidationError("direction 必须是 higher_better 或 lower_better")
            weight = self._number(weight, "weight")
            if weight <= 0:
                raise ValidationError("weight 必须大于 0")
            products = self._products(applicable_products)
            window = self._window(sampling_window)
            method_version = self._text(method_version, "method_version", 80)
            adjustment = self._adjustment(adjustment)
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version FROM metric_versions WHERE metric_id=?",
                (metric_id,),
            ).fetchone()
            version = row["max_version"] + 1
            active = connection.execute(
                "SELECT metric_version_id FROM metric_versions WHERE metric_id=? AND status='active'",
                (metric_id,),
            ).fetchone()
            supersedes = active["metric_version_id"] if active else None

            def create() -> tuple[str, str, dict[str, Any]]:
                metric_version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO metric_versions(metric_version_id,metric_id,version,name,unit,direction,weight,"
                    "applicable_products_json,sampling_window_json,method_version,adjustment_json,status,"
                    "supersedes_version_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)",
                    (metric_version_id, metric_id, version, name, unit, direction, weight,
                     canonical_json(products), canonical_json(window), method_version,
                     canonical_json(adjustment), supersedes, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="metric_version.defined",
                             resource_type="metric_version", resource_id=metric_version_id,
                             detail={"metric_id": metric_id, "version": version,
                                     "method_version": method_version,
                                     "supersedes_version_id": supersedes},
                             occurred_at=self._now())
                return "metric_version", metric_version_id, {"metric_version_id": metric_version_id,
                                                             "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="define_metric_version", payload=payload, create=create)

    def activate_metric_version(self, *, request_id: str, actor_id: str,
                                metric_version_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "metric_version_id": metric_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            version_row = self._metric_version_row(connection, metric_version_id)
            if version_row["status"] != "draft":
                raise ConflictError("只有草稿状态的指标版本可以激活")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE metric_versions SET status='retired' WHERE metric_id=? AND status='active'",
                    (version_row["metric_id"],),
                )
                connection.execute(
                    "UPDATE metric_versions SET status='active' WHERE metric_version_id=?",
                    (metric_version_id,),
                )
                append_event(connection, actor_id=actor_id, action="metric_version.activated",
                             resource_type="metric_version", resource_id=metric_version_id,
                             detail={"metric_id": version_row["metric_id"],
                                     "version": version_row["version"],
                                     "supersedes_version_id": version_row["supersedes_version_id"]},
                             occurred_at=self._now())
                return "metric_version", metric_version_id, {"metric_version_id": metric_version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="activate_metric_version", payload=payload, create=create)

    # ---------- 周期与证据 ----------

    def create_cycle(self, *, request_id: str, actor_id: str, cycle_id: str, name: str,
                     period_start: str, period_end: str,
                     metrics: list[dict[str, str]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "name": name,
                   "period_start": period_start, "period_end": period_end, "metrics": metrics}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            cycle_id = self._identifier(cycle_id, "cycle_id")
            name = self._text(name, "name")
            period_start = self._date(period_start, "period_start")
            period_end = self._date(period_end, "period_end")
            if period_start > period_end:
                raise ValidationError("周期起止日期顺序无效")
            if not isinstance(metrics, list) or not metrics:
                raise ValidationError("metrics 必须是非空数组")
            pinned = []
            seen_metrics = set()
            for item in metrics:
                if not isinstance(item, dict):
                    raise ValidationError("metrics 元素必须是对象")
                metric_id = self._identifier(str(item.get("metric_id", "")), "metric_id")
                if metric_id in seen_metrics:
                    raise ValidationError("同一指标在周期内重复")
                seen_metrics.add(metric_id)
                metric_version_id = str(item.get("metric_version_id", ""))
                version_row = self._metric_version_row(connection, metric_version_id)
                if version_row["metric_id"] != metric_id:
                    raise ValidationError("指标版本与指标编号不匹配")
                if version_row["status"] != "active":
                    raise ValidationError("周期只能锁定启用状态的指标版本")
                pinned.append((metric_id, metric_version_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO ranking_cycles(cycle_id,name,period_start,period_end,status,created_by,"
                        "created_at) VALUES(?,?,?,?,'open',?,?)",
                        (cycle_id, name, period_start, period_end, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("周期编号已经存在") from exc
                for metric_id, metric_version_id in pinned:
                    connection.execute(
                        "INSERT INTO cycle_metrics(cycle_id,metric_id,metric_version_id) VALUES(?,?,?)",
                        (cycle_id, metric_id, metric_version_id),
                    )
                append_event(connection, actor_id=actor_id, action="cycle.created",
                             resource_type="cycle", resource_id=cycle_id,
                             detail={"name": name, "period_start": period_start, "period_end": period_end,
                                     "metrics": [{"metric_id": metric_id, "metric_version_id": version_id}
                                                 for metric_id, version_id in pinned]},
                             occurred_at=self._now())
                return "cycle", cycle_id, {"cycle_id": cycle_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_cycle", payload=payload, create=create)

    def submit_evidence(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str,
                        metric_id: str, measurements: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id,
                   "metric_id": metric_id, "measurements": measurements}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._require_site_org(actor, site)
            cycle = self._cycle_row(connection, cycle_id)
            if cycle["status"] != "open":
                raise ConflictError("周期已冻结，停止接收新的证据")
            metric_id = self._identifier(metric_id, "metric_id")
            pinned = self._pinned_version(connection, cycle_id, metric_id)
            if pinned is None:
                raise NotFoundError("该指标未纳入此排名周期")
            products = json.loads(pinned["applicable_products_json"])
            window = json.loads(pinned["sampling_window_json"])
            validated = self._measurements(measurements, products, window)
            data_hash = digest(validated)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM evidence_submissions WHERE cycle_id=? AND site_id=? AND metric_id=?",
                    (cycle_id, site_id, metric_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE evidence_submissions SET measurements_json=?, payload_hash=?, submitted_by=?, "
                        "updated_at=? WHERE submission_id=?",
                        (canonical_json(validated), data_hash, actor_id, self._now(),
                         existing["submission_id"]),
                    )
                    append_event(connection, actor_id=actor_id, action="evidence.updated",
                                 resource_type="submission", resource_id=existing["submission_id"],
                                 detail={"cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id,
                                         "payload_hash": data_hash},
                                 occurred_at=self._now())
                    return "submission", existing["submission_id"], {
                        "submission_id": existing["submission_id"], "updated": True}
                submission_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO evidence_submissions(submission_id,cycle_id,site_id,metric_id,measurements_json,"
                    "payload_hash,submitted_by,submitted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (submission_id, cycle_id, site_id, metric_id, canonical_json(validated), data_hash,
                     actor_id, now, now),
                )
                append_event(connection, actor_id=actor_id, action="evidence.submitted",
                             resource_type="submission", resource_id=submission_id,
                             detail={"cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id,
                                     "payload_hash": data_hash},
                             occurred_at=self._now())
                return "submission", submission_id, {"submission_id": submission_id, "updated": False}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_evidence", payload=payload, create=create)

    def freeze_cycle(self, *, request_id: str, actor_id: str, cycle_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            cycle = self._cycle_row(connection, cycle_id)
            if cycle["status"] != "open":
                raise ConflictError("只有开放中的周期可以冻结")

            def create() -> tuple[str, str, dict[str, Any]]:
                submissions = connection.execute(
                    "SELECT * FROM evidence_submissions WHERE cycle_id=? ORDER BY site_id, metric_id",
                    (cycle_id,),
                ).fetchall()
                count = 0
                for submission in submissions:
                    pinned = self._pinned_version(connection, cycle_id, submission["metric_id"])
                    measurements = json.loads(submission["measurements_json"])
                    raw, adjusted, computation = self._compute_scores(pinned, measurements)
                    connection.execute(
                        "INSERT INTO score_snapshots(snapshot_id,cycle_id,site_id,metric_id,metric_version_id,"
                        "raw_score,adjusted_score,computation_json,excluded_batches_json,excluded,suspended,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,0,0,?)",
                        (uuid.uuid4().hex, cycle_id, submission["site_id"], submission["metric_id"],
                         pinned["metric_version_id"], raw, adjusted, canonical_json(computation),
                         "[]", self._now()),
                    )
                    count += 1
                connection.execute(
                    "UPDATE ranking_cycles SET status='frozen', frozen_at=? WHERE cycle_id=?",
                    (self._now(), cycle_id),
                )
                append_event(connection, actor_id=actor_id, action="cycle.frozen",
                             resource_type="cycle", resource_id=cycle_id,
                             detail={"snapshots": count}, occurred_at=self._now())
                return "cycle", cycle_id, {"cycle_id": cycle_id, "snapshots": count}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_cycle", payload=payload, create=create)

    # ---------- 异常值排除 ----------

    def request_exclusion(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str,
                          metric_id: str, batch_ids: list[str], reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id,
                   "metric_id": metric_id, "batch_ids": batch_ids, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._require_site_org(actor, site)
            cycle = self._cycle_row(connection, cycle_id)
            if cycle["status"] == "open":
                raise ConflictError("周期尚未冻结，不能申请异常值排除")
            metric_id = self._identifier(metric_id, "metric_id")
            reason = self._text(reason, "reason", 500)
            snapshot = self._snapshot_row(connection, cycle_id, site_id, metric_id)
            if snapshot["excluded"]:
                raise ConflictError("该分数已整体排除")
            if not isinstance(batch_ids, list) or not batch_ids:
                raise ValidationError("batch_ids 必须是非空数组")
            requested = [self._identifier(str(batch_id), "batch_ids") for batch_id in batch_ids]
            if len(set(requested)) != len(requested):
                raise ValidationError("batch_ids 存在重复")
            submission = connection.execute(
                "SELECT * FROM evidence_submissions WHERE cycle_id=? AND site_id=? AND metric_id=?",
                (cycle_id, site_id, metric_id),
            ).fetchone()
            known = {item["batch_id"] for item in json.loads(submission["measurements_json"])}
            unknown = sorted(set(requested) - known)
            if unknown:
                raise ValidationError(f"批次不在已提交证据中: {unknown}")
            already = set(json.loads(snapshot["excluded_batches_json"]))
            overlap = sorted(already & set(requested))
            if overlap:
                raise ConflictError(f"批次已被排除: {overlap}")
            pending = connection.execute(
                "SELECT 1 FROM exclusion_requests WHERE cycle_id=? AND site_id=? AND metric_id=? "
                "AND status='pending'",
                (cycle_id, site_id, metric_id),
            ).fetchone()
            if pending:
                raise ConflictError("存在待审查的排除申请")

            def create() -> tuple[str, str, dict[str, Any]]:
                exclusion_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO exclusion_requests(exclusion_id,cycle_id,site_id,metric_id,batch_ids_json,"
                    "reason,status,requested_by,requested_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                    (exclusion_id, cycle_id, site_id, metric_id, canonical_json(requested), reason,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="exclusion.requested",
                             resource_type="exclusion", resource_id=exclusion_id,
                             detail={"cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id,
                                     "batch_ids": requested},
                             occurred_at=self._now())
                return "exclusion", exclusion_id, {"exclusion_id": exclusion_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="request_exclusion", payload=payload, create=create)

    def review_exclusion(self, *, request_id: str, actor_id: str, exclusion_id: str,
                         approve: bool, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "exclusion_id": exclusion_id, "approve": approve, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            approve = self._flag(approve, "approve")
            exclusion = connection.execute(
                "SELECT * FROM exclusion_requests WHERE exclusion_id=?", (exclusion_id,)
            ).fetchone()
            if exclusion is None:
                raise NotFoundError("排除申请不存在")
            site = self._site_row(connection, exclusion["site_id"])
            self._require_independent_reviewer(actor, site["organization_id"], exclusion["requested_by"])
            if exclusion["status"] != "pending":
                raise ConflictError("排除申请已审查")
            note = str(note).strip()
            if not approve and not note:
                raise ValidationError("驳回申请必须说明理由")
            if len(note) > 500:
                raise ValidationError("note 不能超过 500 个字符")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                if not approve:
                    connection.execute(
                        "UPDATE exclusion_requests SET status='rejected', reviewed_by=?, reviewed_at=?, "
                        "review_note=? WHERE exclusion_id=?",
                        (actor_id, now, note, exclusion_id),
                    )
                    append_event(connection, actor_id=actor_id, action="exclusion.rejected",
                                 resource_type="exclusion", resource_id=exclusion_id,
                                 detail={"note": note}, occurred_at=now)
                    return "exclusion", exclusion_id, {"exclusion_id": exclusion_id, "status": "rejected"}
                cycle_id = exclusion["cycle_id"]
                site_id = exclusion["site_id"]
                metric_id = exclusion["metric_id"]
                metric_before = self._metric_ranking(connection, cycle_id, metric_id)
                totals_before = self._cycle_totals(connection, cycle_id)
                snapshot = self._snapshot_row(connection, cycle_id, site_id, metric_id)
                submission = connection.execute(
                    "SELECT * FROM evidence_submissions WHERE cycle_id=? AND site_id=? AND metric_id=?",
                    (cycle_id, site_id, metric_id),
                ).fetchone()
                measurements = json.loads(submission["measurements_json"])
                excluded_batches = set(json.loads(snapshot["excluded_batches_json"]))
                excluded_batches.update(json.loads(exclusion["batch_ids_json"]))
                remaining = [item for item in measurements if item["batch_id"] not in excluded_batches]
                pinned = self._pinned_version(connection, cycle_id, metric_id)
                if remaining:
                    raw, adjusted, computation = self._compute_scores(pinned, measurements, excluded_batches)
                    connection.execute(
                        "UPDATE score_snapshots SET raw_score=?, adjusted_score=?, computation_json=?, "
                        "excluded_batches_json=?, excluded=0 WHERE snapshot_id=?",
                        (raw, adjusted, canonical_json(computation),
                         canonical_json(sorted(excluded_batches)), snapshot["snapshot_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE score_snapshots SET excluded=1, excluded_batches_json=? WHERE snapshot_id=?",
                        (canonical_json(sorted(excluded_batches)), snapshot["snapshot_id"]),
                    )
                metric_after = self._metric_ranking(connection, cycle_id, metric_id)
                totals_after = self._cycle_totals(connection, cycle_id)
                impact = {"metric_id": metric_id,
                          "metric_ranks_before": metric_before,
                          "metric_ranks_after": metric_after,
                          "totals_before": totals_before,
                          "totals_after": totals_after}
                connection.execute(
                    "UPDATE exclusion_requests SET status='approved', reviewed_by=?, reviewed_at=?, "
                    "review_note=?, impact_json=? WHERE exclusion_id=?",
                    (actor_id, now, note, canonical_json(impact), exclusion_id),
                )
                append_event(connection, actor_id=actor_id, action="exclusion.approved",
                             resource_type="exclusion", resource_id=exclusion_id,
                             detail={"cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id,
                                     "excluded_batches": sorted(excluded_batches),
                                     "rank_before": metric_before.get(site_id),
                                     "rank_after": metric_after.get(site_id)},
                             occurred_at=now)
                return "exclusion", exclusion_id, {"exclusion_id": exclusion_id, "status": "approved"}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_exclusion", payload=payload, create=create)

    # ---------- 异议处理 ----------

    def raise_dispute(self, *, request_id: str, actor_id: str, cycle_id: str, site_id: str,
                      metric_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id, "site_id": site_id,
                   "metric_id": metric_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._require_site_org(actor, site)
            cycle = self._cycle_row(connection, cycle_id)
            if cycle["status"] == "open":
                raise ConflictError("周期尚未冻结，不能提出异议")
            metric_id = self._identifier(metric_id, "metric_id")
            reason = self._text(reason, "reason", 500)
            self._snapshot_row(connection, cycle_id, site_id, metric_id)
            open_dispute = connection.execute(
                "SELECT 1 FROM disputes WHERE cycle_id=? AND site_id=? AND metric_id=? AND status='open'",
                (cycle_id, site_id, metric_id),
            ).fetchone()
            if open_dispute:
                raise ConflictError("存在未决异议")

            def create() -> tuple[str, str, dict[str, Any]]:
                dispute_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO disputes(dispute_id,cycle_id,site_id,metric_id,reason,status,raised_by,"
                    "raised_at) VALUES(?,?,?,?,?,'open',?,?)",
                    (dispute_id, cycle_id, site_id, metric_id, reason, actor_id, now),
                )
                suspended_version = None
                if cycle["status"] == "published":
                    latest = connection.execute(
                        "SELECT * FROM metric_publications WHERE cycle_id=? AND metric_id=? "
                        "ORDER BY version DESC LIMIT 1",
                        (cycle_id, metric_id),
                    ).fetchone()
                    if latest and latest["status"] == "published":
                        suspended_version = latest["version"] + 1
                        connection.execute(
                            "INSERT INTO metric_publications(publication_id,cycle_id,metric_id,version,status,"
                            "reason,published_by,published_at) VALUES(?,?,?,?,'suspended',?,?,?)",
                            (uuid.uuid4().hex, cycle_id, metric_id, suspended_version,
                             f"异议 {dispute_id} 待处理", actor_id, now),
                        )
                        append_event(connection, actor_id=actor_id, action="metric.publication_suspended",
                                     resource_type="metric_publication", resource_id=f"{cycle_id}:{metric_id}",
                                     detail={"cycle_id": cycle_id, "metric_id": metric_id,
                                             "version": suspended_version, "dispute_id": dispute_id},
                                     occurred_at=now)
                append_event(connection, actor_id=actor_id, action="dispute.raised",
                             resource_type="dispute", resource_id=dispute_id,
                             detail={"cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id,
                                     "suspended_publication_version": suspended_version},
                             occurred_at=now)
                return "dispute", dispute_id, {"dispute_id": dispute_id,
                                               "suspended_publication_version": suspended_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="raise_dispute", payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        outcome: str, note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "outcome": outcome, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            dispute = connection.execute(
                "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if dispute is None:
                raise NotFoundError("异议不存在")
            site = self._site_row(connection, dispute["site_id"])
            self._require_independent_reviewer(actor, site["organization_id"], dispute["raised_by"])
            if dispute["status"] != "open":
                raise ConflictError("异议已处理")
            if outcome not in DISPUTE_OUTCOMES:
                raise ValidationError("outcome 必须是 upheld 或 rejected")
            note = self._text(note, "note", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                status = "resolved_upheld" if outcome == "upheld" else "resolved_rejected"
                now = self._now()
                connection.execute(
                    "UPDATE disputes SET status=?, resolved_by=?, resolved_at=?, resolution_note=? "
                    "WHERE dispute_id=?",
                    (status, actor_id, now, note, dispute_id),
                )
                if outcome == "upheld":
                    connection.execute(
                        "UPDATE score_snapshots SET suspended=1 WHERE cycle_id=? AND site_id=? AND metric_id=?",
                        (dispute["cycle_id"], dispute["site_id"], dispute["metric_id"]),
                    )
                append_event(connection, actor_id=actor_id, action="dispute.resolved",
                             resource_type="dispute", resource_id=dispute_id,
                             detail={"outcome": outcome, "note": note}, occurred_at=now)
                return "dispute", dispute_id, {"dispute_id": dispute_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_dispute", payload=payload, create=create)

    # ---------- 发布 ----------

    def publish_cycle(self, *, request_id: str, actor_id: str, cycle_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "cycle_id": cycle_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            cycle = self._cycle_row(connection, cycle_id)
            if cycle["status"] == "open":
                raise ConflictError("周期尚未冻结，不能发布")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                metrics = connection.execute(
                    "SELECT * FROM cycle_metrics WHERE cycle_id=? ORDER BY metric_id", (cycle_id,)
                ).fetchall()
                statuses: dict[str, str] = {}
                published_metric_ids: list[str] = []
                for cycle_metric in metrics:
                    metric_id = cycle_metric["metric_id"]
                    open_disputes = connection.execute(
                        "SELECT COUNT(*) AS count FROM disputes WHERE cycle_id=? AND metric_id=? "
                        "AND status='open'",
                        (cycle_id, metric_id),
                    ).fetchone()["count"]
                    row = connection.execute(
                        "SELECT COALESCE(MAX(version), 0) AS max_version FROM metric_publications "
                        "WHERE cycle_id=? AND metric_id=?",
                        (cycle_id, metric_id),
                    ).fetchone()
                    version = row["max_version"] + 1
                    publication_id = uuid.uuid4().hex
                    if open_disputes:
                        connection.execute(
                            "INSERT INTO metric_publications(publication_id,cycle_id,metric_id,version,status,"
                            "reason,published_by,published_at) VALUES(?,?,?,?,'suspended',?,?,?)",
                            (publication_id, cycle_id, metric_id, version, "存在未决异议", actor_id, now),
                        )
                        statuses[metric_id] = "suspended"
                        continue
                    connection.execute(
                        "INSERT INTO metric_publications(publication_id,cycle_id,metric_id,version,status,"
                        "reason,published_by,published_at) VALUES(?,?,?,?,'published',NULL,?,?)",
                        (publication_id, cycle_id, metric_id, version, actor_id, now),
                    )
                    ranking = self._metric_ranking(connection, cycle_id, metric_id)
                    snapshots = connection.execute(
                        "SELECT * FROM score_snapshots WHERE cycle_id=? AND metric_id=? ORDER BY site_id",
                        (cycle_id, metric_id),
                    ).fetchall()
                    for snapshot in snapshots:
                        active = not snapshot["excluded"] and not snapshot["suspended"]
                        entry = ranking.get(snapshot["site_id"]) if active else None
                        connection.execute(
                            "INSERT INTO publication_entries(publication_id,site_id,adjusted_score,"
                            "rank_position,included) VALUES(?,?,?,?,?)",
                            (publication_id, snapshot["site_id"],
                             entry["adjusted_score"] if entry else None,
                             entry["rank"] if entry else None,
                             1 if active else 0),
                        )
                    statuses[metric_id] = "published"
                    published_metric_ids.append(metric_id)
                totals = self._cycle_totals(connection, cycle_id, published_metric_ids)
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS max_version FROM total_publications WHERE cycle_id=?",
                    (cycle_id,),
                ).fetchone()
                total_version = row["max_version"] + 1
                total_publication_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO total_publications(publication_id,cycle_id,version,published_by,published_at) "
                    "VALUES(?,?,?,?,?)",
                    (total_publication_id, cycle_id, total_version, actor_id, now),
                )
                for site_id, total in totals.items():
                    connection.execute(
                        "INSERT INTO total_entries(publication_id,site_id,total_score,rank_position) "
                        "VALUES(?,?,?,?)",
                        (total_publication_id, site_id, total["total_score"], total["rank"]),
                    )
                connection.execute(
                    "UPDATE ranking_cycles SET status='published', "
                    "published_at=COALESCE(published_at, ?) WHERE cycle_id=?",
                    (now, cycle_id),
                )
                append_event(connection, actor_id=actor_id, action="cycle.published",
                             resource_type="cycle", resource_id=cycle_id,
                             detail={"metrics": statuses, "total_version": total_version},
                             occurred_at=now)
                return "cycle", cycle_id, {"cycle_id": cycle_id, "metrics": statuses,
                                           "total_version": total_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_cycle", payload=payload, create=create)

    # ---------- 整改闭环 ----------

    def create_corrective_plan(self, *, request_id: str, actor_id: str, finding_type: str,
                               finding_id: str, site_id: str, owner_actor_id: str,
                               description: str, deadline: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "finding_type": finding_type, "finding_id": finding_id,
                   "site_id": site_id, "owner_actor_id": owner_actor_id,
                   "description": description, "deadline": deadline}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._require_site_org(actor, site)
            if finding_type not in FINDING_TYPES:
                raise ValidationError("finding_type 必须是 metric_result、dispute 或 exclusion")
            finding_id = self._identifier(finding_id, "finding_id")
            if finding_type == "metric_result":
                finding = connection.execute(
                    "SELECT * FROM score_snapshots WHERE snapshot_id=?", (finding_id,)
                ).fetchone()
                if finding is None:
                    raise NotFoundError("关联的指标结果不存在")
                if finding["site_id"] != site_id:
                    raise ValidationError("关联发现不属于该工厂")
            elif finding_type == "dispute":
                finding = connection.execute(
                    "SELECT * FROM disputes WHERE dispute_id=?", (finding_id,)
                ).fetchone()
                if finding is None:
                    raise NotFoundError("关联的异议不存在")
                if finding["site_id"] != site_id:
                    raise ValidationError("关联发现不属于该工厂")
            else:
                finding = connection.execute(
                    "SELECT * FROM exclusion_requests WHERE exclusion_id=?", (finding_id,)
                ).fetchone()
                if finding is None:
                    raise NotFoundError("关联的排除申请不存在")
                if finding["site_id"] != site_id:
                    raise ValidationError("关联发现不属于该工厂")
            owner = self._actor(connection, owner_actor_id)
            if owner.organization_id != site["organization_id"]:
                raise ValidationError("责任人必须属于工厂所属组织")
            description = self._text(description, "description", 500)
            deadline = self._date(deadline, "deadline")
            if deadline < self._now()[:10]:
                raise ValidationError("整改期限不能早于当前日期")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO corrective_plans(plan_id,finding_type,finding_id,site_id,owner_actor_id,"
                    "description,deadline,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'open',?,?)",
                    (plan_id, finding_type, finding_id, site_id, owner_actor_id, description, deadline,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="plan.created",
                             resource_type="plan", resource_id=plan_id,
                             detail={"finding_type": finding_type, "finding_id": finding_id,
                                     "site_id": site_id, "owner_actor_id": owner_actor_id,
                                     "deadline": deadline},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_corrective_plan", payload=payload, create=create)

    def submit_plan_evidence(self, *, request_id: str, actor_id: str, plan_id: str,
                             description: str, evidence: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id,
                   "description": description, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            plan = connection.execute(
                "SELECT * FROM corrective_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("整改计划不存在")
            if actor.actor_id != plan["owner_actor_id"]:
                self._require(actor, "admin", "operator")
                site = self._site_row(connection, plan["site_id"])
                self._require_site_org(actor, site)
            if plan["status"] == "verified":
                raise ConflictError("整改计划已验证，停止补充证据")
            description = self._text(description, "description", 500)
            if not isinstance(evidence, dict) or not evidence:
                raise ValidationError("evidence 必须是非空对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO plan_evidence(evidence_id,plan_id,description,payload_json,submitted_by,"
                    "submitted_at) VALUES(?,?,?,?,?,?)",
                    (evidence_id, plan_id, description, canonical_json(evidence), actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE corrective_plans SET status='submitted' WHERE plan_id=?", (plan_id,)
                )
                append_event(connection, actor_id=actor_id, action="plan.evidence_submitted",
                             resource_type="plan", resource_id=plan_id,
                             detail={"evidence_id": evidence_id, "payload_hash": digest(evidence)},
                             occurred_at=self._now())
                return "plan_evidence", evidence_id, {"evidence_id": evidence_id, "plan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_plan_evidence", payload=payload, create=create)

    def verify_corrective_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                               approve: bool, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "approve": approve, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            approve = self._flag(approve, "approve")
            plan = connection.execute(
                "SELECT * FROM corrective_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("整改计划不存在")
            site = self._site_row(connection, plan["site_id"])
            self._require_independent_reviewer(actor, site["organization_id"], plan["owner_actor_id"])
            if plan["status"] != "submitted":
                raise ConflictError("整改计划尚未提交复验证据")
            note = str(note).strip()
            if not approve and not note:
                raise ValidationError("退回整改必须说明理由")
            if len(note) > 500:
                raise ValidationError("note 不能超过 500 个字符")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 整改验证只更新计划状态，不改写任何冻结分数与已发布排名。
                now = self._now()
                if approve:
                    connection.execute(
                        "UPDATE corrective_plans SET status='verified', verified_by=?, verified_at=? "
                        "WHERE plan_id=?",
                        (actor_id, now, plan_id),
                    )
                    append_event(connection, actor_id=actor_id, action="plan.verified",
                                 resource_type="plan", resource_id=plan_id,
                                 detail={"note": note}, occurred_at=now)
                    return "plan", plan_id, {"plan_id": plan_id, "status": "verified"}
                connection.execute(
                    "UPDATE corrective_plans SET status='open' WHERE plan_id=?", (plan_id,)
                )
                append_event(connection, actor_id=actor_id, action="plan.returned",
                             resource_type="plan", resource_id=plan_id,
                             detail={"note": note}, occurred_at=now)
                return "plan", plan_id, {"plan_id": plan_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="verify_corrective_plan", payload=payload, create=create)

    # ---------- 权限授予与批次放行 ----------

    def grant_permission(self, *, request_id: str, actor_id: str, target_actor_id: str,
                         permission: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "target_actor_id": target_actor_id, "permission": permission}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._actor(connection, target_actor_id)
            if permission not in PERMISSIONS:
                raise ValidationError(f"permission 必须是 {sorted(PERMISSIONS)} 之一")

            def create() -> tuple[str, str, dict[str, Any]]:
                resource_id = f"{target_actor_id}:{permission}"
                existing = connection.execute(
                    "SELECT 1 FROM permission_grants WHERE actor_id=? AND permission=?",
                    (target_actor_id, permission),
                ).fetchone()
                if existing:
                    return "permission", resource_id, {"actor_id": target_actor_id,
                                                       "permission": permission}
                connection.execute(
                    "INSERT INTO permission_grants(actor_id,permission,granted_by,granted_at) VALUES(?,?,?,?)",
                    (target_actor_id, permission, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="permission.granted",
                             resource_type="permission", resource_id=resource_id,
                             detail={"target_actor_id": target_actor_id, "permission": permission},
                             occurred_at=self._now())
                return "permission", resource_id, {"actor_id": target_actor_id, "permission": permission}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_permission", payload=payload, create=create)

    def record_release_decision(self, *, request_id: str, actor_id: str, site_id: str,
                                batch_id: str, decision: str, basis: dict[str, Any]) -> WriteReceipt:
        # 批次放行只依据独立的放行材料，结构上不读取任何排名分数。
        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id,
                   "decision": decision, "basis": basis}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_permission(connection, actor, "batch.release")
            self._site_row(connection, site_id)
            batch_id = self._identifier(batch_id, "batch_id")
            if decision not in RELEASE_DECISIONS:
                raise ValidationError("decision 必须是 released 或 rejected")
            if not isinstance(basis, dict) or not basis:
                raise ValidationError("basis 必须是非空对象，记录放行依据")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT 1 FROM release_decisions WHERE site_id=? AND batch_id=?",
                    (site_id, batch_id),
                ).fetchone()
                if existing:
                    raise ConflictError("该批次已有放行决定")
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO release_decisions(decision_id,site_id,batch_id,decision,basis_json,"
                    "decided_by,decided_at) VALUES(?,?,?,?,?,?,?)",
                    (decision_id, site_id, batch_id, decision, canonical_json(basis),
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="release.decided",
                             resource_type="release_decision", resource_id=decision_id,
                             detail={"site_id": site_id, "batch_id": batch_id, "decision": decision},
                             occurred_at=self._now())
                return "release_decision", decision_id, {"decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_release_decision", payload=payload, create=create)

    # ---------- 查询 ----------

    def _metric_version_dict(self, row) -> dict[str, Any]:
        return {"metric_version_id": row["metric_version_id"], "metric_id": row["metric_id"],
                "version": row["version"], "name": row["name"], "unit": row["unit"],
                "direction": row["direction"], "weight": row["weight"],
                "applicable_products": json.loads(row["applicable_products_json"]),
                "sampling_window": json.loads(row["sampling_window_json"]),
                "method_version": row["method_version"],
                "adjustment": json.loads(row["adjustment_json"]),
                "status": row["status"], "supersedes_version_id": row["supersedes_version_id"],
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def list_metric_versions(self, metric_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM metric_versions"
        parameters: list[Any] = []
        if metric_id:
            query += " WHERE metric_id=?"
            parameters.append(metric_id)
        query += " ORDER BY metric_id, version"
        return [self._metric_version_dict(row)
                for row in self.database.connection.execute(query, parameters)]

    def get_cycle(self, cycle_id: str) -> dict[str, Any]:
        connection = self.database.connection
        cycle = self._cycle_row(connection, cycle_id)
        metrics = connection.execute(
            "SELECT cm.metric_id, cm.metric_version_id, mv.name, mv.version, mv.method_version, mv.weight "
            "FROM cycle_metrics cm JOIN metric_versions mv ON mv.metric_version_id=cm.metric_version_id "
            "WHERE cm.cycle_id=? ORDER BY cm.metric_id",
            (cycle_id,),
        ).fetchall()
        return {"cycle_id": cycle["cycle_id"], "name": cycle["name"],
                "period_start": cycle["period_start"], "period_end": cycle["period_end"],
                "status": cycle["status"], "frozen_at": cycle["frozen_at"],
                "published_at": cycle["published_at"],
                "metrics": [dict(row) for row in metrics]}

    def get_ranking(self, *, actor_id: str, cycle_id: str,
                    metric_id: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require_permission(connection, actor, "ranking.view")
        cycle = self._cycle_row(connection, cycle_id)
        query = "SELECT metric_id FROM cycle_metrics WHERE cycle_id=?"
        parameters: list[Any] = [cycle_id]
        if metric_id:
            query += " AND metric_id=?"
            parameters.append(metric_id)
        query += " ORDER BY metric_id"
        metrics = connection.execute(query, parameters).fetchall()
        if metric_id and not metrics:
            raise NotFoundError("该指标未纳入此排名周期")
        result_metrics = []
        for cycle_metric in metrics:
            publication = connection.execute(
                "SELECT * FROM metric_publications WHERE cycle_id=? AND metric_id=? "
                "ORDER BY version DESC LIMIT 1",
                (cycle_id, cycle_metric["metric_id"]),
            ).fetchone()
            entries = []
            if publication and publication["status"] == "published":
                rows = connection.execute(
                    "SELECT * FROM publication_entries WHERE publication_id=? "
                    "ORDER BY included DESC, rank_position, site_id",
                    (publication["publication_id"],),
                ).fetchall()
                entries = [{"site_id": row["site_id"], "adjusted_score": row["adjusted_score"],
                            "rank": row["rank_position"], "included": bool(row["included"])}
                           for row in rows]
            result_metrics.append({
                "metric_id": cycle_metric["metric_id"],
                "publication": None if publication is None else {
                    "version": publication["version"], "status": publication["status"],
                    "reason": publication["reason"], "published_at": publication["published_at"]},
                "entries": entries,
            })
        total_publication = connection.execute(
            "SELECT * FROM total_publications WHERE cycle_id=? ORDER BY version DESC LIMIT 1",
            (cycle_id,),
        ).fetchone()
        totals = None
        if total_publication:
            rows = connection.execute(
                "SELECT * FROM total_entries WHERE publication_id=? ORDER BY rank_position, site_id",
                (total_publication["publication_id"],),
            ).fetchall()
            totals = {"version": total_publication["version"],
                      "published_at": total_publication["published_at"],
                      "entries": [{"site_id": row["site_id"], "total_score": row["total_score"],
                                   "rank": row["rank_position"]} for row in rows]}
        return {"cycle_id": cycle_id, "cycle_status": cycle["status"],
                "metrics": result_metrics, "totals": totals}

    def get_scorecard(self, *, actor_id: str, cycle_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        site = self._site_row(connection, site_id)
        if (actor.role != "admin" and actor.organization_id != site["organization_id"]
                and not self._has_permission(connection, actor, "ranking.view")):
            raise PermissionDenied("只能查看本组织工厂的分数卡")
        self._cycle_row(connection, cycle_id)
        snapshots = connection.execute(
            "SELECT ss.*, mv.name AS metric_name, mv.unit FROM score_snapshots ss "
            "JOIN metric_versions mv ON mv.metric_version_id=ss.metric_version_id "
            "WHERE ss.cycle_id=? AND ss.site_id=? ORDER BY ss.metric_id",
            (cycle_id, site_id),
        ).fetchall()
        items = []
        for snapshot in snapshots:
            publication = connection.execute(
                "SELECT status FROM metric_publications WHERE cycle_id=? AND metric_id=? "
                "ORDER BY version DESC LIMIT 1",
                (cycle_id, snapshot["metric_id"]),
            ).fetchone()
            items.append({"metric_id": snapshot["metric_id"],
                          "metric_name": snapshot["metric_name"], "unit": snapshot["unit"],
                          "raw_score": snapshot["raw_score"],
                          "adjusted_score": snapshot["adjusted_score"],
                          "excluded": bool(snapshot["excluded"]),
                          "suspended": bool(snapshot["suspended"]),
                          "excluded_batches": json.loads(snapshot["excluded_batches_json"]),
                          "publication_status": None if publication is None else publication["status"]})
        return {"cycle_id": cycle_id, "site_id": site_id, "items": items}

    def trace_score(self, *, actor_id: str, cycle_id: str, site_id: str, metric_id: str,
                    purpose: str) -> dict[str, Any]:
        connection = self.database.connection
        if purpose != "ranking":
            raise PermissionDenied("排名数据仅用于质量对标，不能作为产品批次放行依据")
        actor = self._actor(connection, actor_id)
        self._require_permission(connection, actor, "ranking.trace")
        snapshot = self._snapshot_row(connection, cycle_id, site_id, metric_id)
        metric_version = self._metric_version_row(connection, snapshot["metric_version_id"])
        submission = connection.execute(
            "SELECT * FROM evidence_submissions WHERE cycle_id=? AND site_id=? AND metric_id=?",
            (cycle_id, site_id, metric_id),
        ).fetchone()
        exclusions = connection.execute(
            "SELECT * FROM exclusion_requests WHERE cycle_id=? AND site_id=? AND metric_id=? "
            "ORDER BY requested_at",
            (cycle_id, site_id, metric_id),
        ).fetchall()
        disputes = connection.execute(
            "SELECT * FROM disputes WHERE cycle_id=? AND site_id=? AND metric_id=? ORDER BY raised_at",
            (cycle_id, site_id, metric_id),
        ).fetchall()
        publication = connection.execute(
            "SELECT * FROM metric_publications WHERE cycle_id=? AND metric_id=? "
            "ORDER BY version DESC LIMIT 1",
            (cycle_id, metric_id),
        ).fetchone()
        publication_entry = None
        if publication:
            publication_entry = connection.execute(
                "SELECT * FROM publication_entries WHERE publication_id=? AND site_id=?",
                (publication["publication_id"], site_id),
            ).fetchone()
        return {
            "purpose": "ranking",
            "cycle_id": cycle_id, "site_id": site_id, "metric_id": metric_id,
            "score": {"snapshot_id": snapshot["snapshot_id"],
                      "raw_score": snapshot["raw_score"],
                      "adjusted_score": snapshot["adjusted_score"],
                      "excluded": bool(snapshot["excluded"]),
                      "suspended": bool(snapshot["suspended"]),
                      "excluded_batches": json.loads(snapshot["excluded_batches_json"]),
                      "computation": json.loads(snapshot["computation_json"])},
            "metric_version": self._metric_version_dict(metric_version),
            "evidence": None if submission is None else {
                "submission_id": submission["submission_id"],
                "measurements": json.loads(submission["measurements_json"]),
                "payload_hash": submission["payload_hash"],
                "submitted_by": submission["submitted_by"],
                "submitted_at": submission["submitted_at"],
                "updated_at": submission["updated_at"]},
            "exclusions": [self._exclusion_dict(row) for row in exclusions],
            "disputes": [self._dispute_dict(row) for row in disputes],
            "publication": None if publication is None else {
                "version": publication["version"], "status": publication["status"],
                "entry": None if publication_entry is None else {
                    "adjusted_score": publication_entry["adjusted_score"],
                    "rank": publication_entry["rank_position"],
                    "included": bool(publication_entry["included"])}},
        }

    def _exclusion_dict(self, row) -> dict[str, Any]:
        return {"exclusion_id": row["exclusion_id"], "cycle_id": row["cycle_id"],
                "site_id": row["site_id"], "metric_id": row["metric_id"],
                "batch_ids": json.loads(row["batch_ids_json"]), "reason": row["reason"],
                "status": row["status"], "requested_by": row["requested_by"],
                "requested_at": row["requested_at"], "reviewed_by": row["reviewed_by"],
                "reviewed_at": row["reviewed_at"], "review_note": row["review_note"],
                "impact": json.loads(row["impact_json"]) if row["impact_json"] else None}

    def _dispute_dict(self, row) -> dict[str, Any]:
        return {"dispute_id": row["dispute_id"], "cycle_id": row["cycle_id"],
                "site_id": row["site_id"], "metric_id": row["metric_id"],
                "reason": row["reason"], "status": row["status"],
                "raised_by": row["raised_by"], "raised_at": row["raised_at"],
                "resolved_by": row["resolved_by"], "resolved_at": row["resolved_at"],
                "resolution_note": row["resolution_note"]}

    def list_exclusions(self, *, actor_id: str, cycle_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        rows = connection.execute(
            "SELECT * FROM exclusion_requests WHERE cycle_id=? ORDER BY requested_at, exclusion_id",
            (cycle_id,),
        ).fetchall()
        return [self._exclusion_dict(row) for row in rows]

    def list_disputes(self, *, actor_id: str, cycle_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        rows = connection.execute(
            "SELECT * FROM disputes WHERE cycle_id=? ORDER BY raised_at, dispute_id", (cycle_id,)
        ).fetchall()
        return [self._dispute_dict(row) for row in rows]

    def list_plans(self, *, actor_id: str, site_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        query = "SELECT * FROM corrective_plans"
        parameters: list[Any] = []
        if site_id:
            query += " WHERE site_id=?"
            parameters.append(site_id)
        query += " ORDER BY created_at, plan_id"
        plans = []
        for row in connection.execute(query, parameters):
            evidence = connection.execute(
                "SELECT * FROM plan_evidence WHERE plan_id=? ORDER BY submitted_at, evidence_id",
                (row["plan_id"],),
            ).fetchall()
            plans.append({"plan_id": row["plan_id"], "finding_type": row["finding_type"],
                          "finding_id": row["finding_id"], "site_id": row["site_id"],
                          "owner_actor_id": row["owner_actor_id"], "description": row["description"],
                          "deadline": row["deadline"], "status": row["status"],
                          "created_by": row["created_by"], "created_at": row["created_at"],
                          "verified_by": row["verified_by"], "verified_at": row["verified_at"],
                          "evidence": [{"evidence_id": item["evidence_id"],
                                        "description": item["description"],
                                        "payload": json.loads(item["payload_json"]),
                                        "submitted_by": item["submitted_by"],
                                        "submitted_at": item["submitted_at"]}
                                       for item in evidence]})
        return plans

    def list_release_decisions(self, *, actor_id: str, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        site = self._site_row(connection, site_id)
        if (actor.role != "admin" and actor.organization_id != site["organization_id"]
                and not self._has_permission(connection, actor, "batch.release")):
            raise PermissionDenied("只能查看本组织工厂的放行决定")
        rows = connection.execute(
            "SELECT * FROM release_decisions WHERE site_id=? ORDER BY decided_at, decision_id",
            (site_id,),
        ).fetchall()
        return [{"decision_id": row["decision_id"], "site_id": row["site_id"],
                 "batch_id": row["batch_id"], "decision": row["decision"],
                 "basis": json.loads(row["basis_json"]), "decided_by": row["decided_by"],
                 "decided_at": row["decided_at"]} for row in rows]
