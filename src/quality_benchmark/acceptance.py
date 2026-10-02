"""跨工厂质量对标平台的离线端到端验收。

在临时 SQLite 数据库中完整走一遍：主体建档 → 指标定义与方法换版 → 周期创建 →
证据提交 → 冻结 → 异常值排除（独立审查，保留名次影响）→ 异议暂停争议指标 →
发布 → 整改闭环 → 权限分离与分数追溯，并核对关键业务不变量。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import PermissionDenied

from .service import QualityService
from .storage import QualityDatabase


def run() -> dict[str, object]:
    """执行完整业务链并返回验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = QualityDatabase(Path(directory) / "acceptance.sqlite3")
        service = QualityService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        checks: dict[str, bool] = {}

        # 主体、操作者与工厂场所
        service.register_organization(request_id="org-hq", actor_id="bootstrap",
                                      organization_id="hq", name="集团质量中心")
        service.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="qm-admin",
                               display_name="质量管理员", role="admin", organization_id="hq")
        service.register_organization(request_id="org-fa", actor_id="qm-admin",
                                      organization_id="fa", name="甲酒厂")
        service.register_organization(request_id="org-fb", actor_id="qm-admin",
                                      organization_id="fb", name="乙酒厂")
        service.register_actor(request_id="actor-reviewer", actor_id="qm-admin", new_actor_id="reviewer-1",
                               display_name="独立审查人", role="reviewer", organization_id="hq")
        service.register_actor(request_id="actor-exec", actor_id="qm-admin", new_actor_id="exec-1",
                               display_name="管理层", role="auditor", organization_id="hq")
        service.register_actor(request_id="actor-release", actor_id="qm-admin", new_actor_id="qa-release",
                               display_name="放行负责人", role="operator", organization_id="hq")
        service.register_actor(request_id="actor-opa", actor_id="qm-admin", new_actor_id="op-a",
                               display_name="甲厂质量员", role="operator", organization_id="fa")
        service.register_actor(request_id="actor-opb", actor_id="qm-admin", new_actor_id="op-b",
                               display_name="乙厂质量员", role="operator", organization_id="fb")
        service.register_site(request_id="site-a", actor_id="qm-admin", site_id="site-a",
                              organization_id="fa", name="甲厂生产基地", timezone_name="Asia/Shanghai")
        service.register_site(request_id="site-b", actor_id="qm-admin", site_id="site-b",
                              organization_id="fb", name="乙厂生产基地", timezone_name="Asia/Shanghai")

        # 指标定义与方法换版：甲醇检测方法 GC-2015 换版为 GC-2026
        methanol_v1 = service.define_metric_version(
            request_id="met-v1", actor_id="qm-admin", metric_id="methanol", name="甲醇含量",
            unit="mg/L", direction="lower_better", weight=0.6, applicable_products=["baijiu"],
            sampling_window={"start": "2026-08-01", "end": "2026-09-30"},
            method_version="GC-2015",
            adjustment={"product_coefficients": {"baijiu": 1.0}, "method_offset": 0.0})
        service.activate_metric_version(request_id="met-v1-act", actor_id="qm-admin",
                                        metric_version_id=methanol_v1.resource_id)
        methanol_v2 = service.define_metric_version(
            request_id="met-v2", actor_id="qm-admin", metric_id="methanol", name="甲醇含量",
            unit="mg/L", direction="lower_better", weight=0.6, applicable_products=["baijiu"],
            sampling_window={"start": "2026-08-01", "end": "2026-09-30"},
            method_version="GC-2026",
            adjustment={"product_coefficients": {"baijiu": 1.0}, "method_offset": -0.5})
        service.activate_metric_version(request_id="met-v2-act", actor_id="qm-admin",
                                        metric_version_id=methanol_v2.resource_id)
        sensory_v1 = service.define_metric_version(
            request_id="sen-v1", actor_id="qm-admin", metric_id="sensory", name="感官评分",
            unit="分", direction="higher_better", weight=0.4,
            applicable_products=["baijiu", "fruit_wine"],
            sampling_window={"start": "2026-08-01", "end": "2026-09-30"},
            method_version="SENS-01",
            adjustment={"product_coefficients": {"baijiu": 1.0, "fruit_wine": 0.98}})
        service.activate_metric_version(request_id="sen-v1-act", actor_id="qm-admin",
                                        metric_version_id=sensory_v1.resource_id)
        versions = service.list_metric_versions("methanol")
        checks["method_change_versioned"] = (
            len(versions) == 2 and versions[0]["status"] == "retired"
            and versions[1]["status"] == "active"
            and versions[1]["supersedes_version_id"] == methanol_v1.resource_id)

        # 周期创建并锁定指标版本
        service.create_cycle(request_id="cycle-1", actor_id="qm-admin", cycle_id="cycle-2026q3",
                             name="2026 年三季度全球质量排名", period_start="2026-07-01",
                             period_end="2026-09-30",
                             metrics=[{"metric_id": "methanol", "metric_version_id": methanol_v2.resource_id},
                                      {"metric_id": "sensory", "metric_version_id": sensory_v1.resource_id}])

        # 工厂提交证据（含原始批次引用）
        service.submit_evidence(request_id="sub-a-met", actor_id="op-a", cycle_id="cycle-2026q3",
                                site_id="site-a", metric_id="methanol",
                                measurements=[
                                    {"batch_id": "BA-001", "product": "baijiu", "value": 10.0,
                                     "sampled_at": "2026-09-05"},
                                    {"batch_id": "BA-002", "product": "baijiu", "value": 12.0,
                                     "sampled_at": "2026-09-12"},
                                    {"batch_id": "BA-003", "product": "baijiu", "value": 40.0,
                                     "sampled_at": "2026-09-19"}])
        service.submit_evidence(request_id="sub-b-met", actor_id="op-b", cycle_id="cycle-2026q3",
                                site_id="site-b", metric_id="methanol",
                                measurements=[
                                    {"batch_id": "BB-001", "product": "baijiu", "value": 11.0,
                                     "sampled_at": "2026-09-06"},
                                    {"batch_id": "BB-002", "product": "baijiu", "value": 13.0,
                                     "sampled_at": "2026-09-13"}])
        service.submit_evidence(request_id="sub-a-sen", actor_id="op-a", cycle_id="cycle-2026q3",
                                site_id="site-a", metric_id="sensory",
                                measurements=[
                                    {"batch_id": "BA-001", "product": "baijiu", "value": 90.0,
                                     "sampled_at": "2026-09-05"},
                                    {"batch_id": "BA-002", "product": "baijiu", "value": 92.0,
                                     "sampled_at": "2026-09-12"}])
        service.submit_evidence(request_id="sub-b-sen", actor_id="op-b", cycle_id="cycle-2026q3",
                                site_id="site-b", metric_id="sensory",
                                measurements=[
                                    {"batch_id": "BB-001", "product": "baijiu", "value": 88.0,
                                     "sampled_at": "2026-09-06"}])

        # 冻结：证据与计算结果固化
        service.freeze_cycle(request_id="freeze-1", actor_id="qm-admin", cycle_id="cycle-2026q3")
        cycle = service.get_cycle("cycle-2026q3")
        checks["cycle_frozen"] = cycle["status"] == "frozen"

        # 异常值排除：甲厂申请排除离群批次 BA-003，独立审查人批准
        exclusion = service.request_exclusion(
            request_id="exc-1", actor_id="op-a", cycle_id="cycle-2026q3", site_id="site-a",
            metric_id="methanol", batch_ids=["BA-003"], reason="BA-003 为设备校准期离群批次")
        service.review_exclusion(request_id="exc-1-review", actor_id="reviewer-1",
                                 exclusion_id=exclusion.resource_id, approve=True,
                                 note="校准记录属实，同意排除")
        exclusions = service.list_exclusions(actor_id="qm-admin", cycle_id="cycle-2026q3")
        impact = exclusions[0]["impact"]
        checks["exclusion_impact_recorded"] = (
            exclusions[0]["status"] == "approved"
            and impact["metric_ranks_before"]["site-a"]["rank"] == 2
            and impact["metric_ranks_after"]["site-a"]["rank"] == 1
            and impact["totals_before"]["site-b"]["rank"] == 1
            and impact["totals_after"]["site-a"]["rank"] == 1)

        # 异议：乙厂对感官指标提出异议，发布时只有该指标被暂停
        dispute = service.raise_dispute(request_id="dis-1", actor_id="op-b", cycle_id="cycle-2026q3",
                                        site_id="site-b", metric_id="sensory",
                                        reason="感官评审样本封存链不完整")
        service.publish_cycle(request_id="pub-1", actor_id="qm-admin", cycle_id="cycle-2026q3")
        service.grant_permission(request_id="grant-view", actor_id="qm-admin",
                                 target_actor_id="exec-1", permission="ranking.view")
        service.grant_permission(request_id="grant-trace", actor_id="qm-admin",
                                 target_actor_id="exec-1", permission="ranking.trace")
        service.grant_permission(request_id="grant-release", actor_id="qm-admin",
                                 target_actor_id="qa-release", permission="batch.release")
        ranking = service.get_ranking(actor_id="exec-1", cycle_id="cycle-2026q3")
        status_by_metric = {item["metric_id"]: item["publication"]["status"]
                            for item in ranking["metrics"]}
        checks["dispute_suspends_only_disputed_metric"] = (
            status_by_metric == {"methanol": "published", "sensory": "suspended"})

        # 异议驳回后重新发布，感官指标恢复
        service.resolve_dispute(request_id="dis-1-resolve", actor_id="reviewer-1",
                                dispute_id=dispute.resource_id, outcome="rejected",
                                note="封存链记录补齐，异议不成立")
        service.publish_cycle(request_id="pub-2", actor_id="qm-admin", cycle_id="cycle-2026q3")
        ranking_after = service.get_ranking(actor_id="exec-1", cycle_id="cycle-2026q3")
        status_after = {item["metric_id"]: item["publication"]["status"]
                        for item in ranking_after["metrics"]}
        checks["republish_restores_metric"] = all(
            status == "published" for status in status_after.values())

        # 整改闭环：关联排除发现、责任人与期限，提交复验证据并验证
        plan = service.create_corrective_plan(
            request_id="plan-1", actor_id="op-a", finding_type="exclusion",
            finding_id=exclusion.resource_id, site_id="site-a", owner_actor_id="op-a",
            description="校准期批次隔离与复检流程整改", deadline="2026-10-15")
        service.submit_plan_evidence(request_id="plan-1-ev", actor_id="op-a",
                                     plan_id=plan.resource_id, description="复检报告",
                                     evidence={"report_id": "REC-2026-091", "result": "passed"})
        service.verify_corrective_plan(request_id="plan-1-verify", actor_id="reviewer-1",
                                       plan_id=plan.resource_id, approve=True, note="复验合格")
        ranking_final = service.get_ranking(actor_id="exec-1", cycle_id="cycle-2026q3")
        checks["plan_verification_keeps_published_ranking"] = ranking_after == ranking_final

        # 追溯：管理层从分数追溯到原始批次与计算规则
        trace = service.trace_score(actor_id="exec-1", cycle_id="cycle-2026q3", site_id="site-a",
                                    metric_id="methanol", purpose="ranking")
        checks["trace_reaches_batches_and_rules"] = (
            trace["score"]["computation"]["batches"] == ["BA-001", "BA-002"]
            and trace["score"]["computation"]["excluded_batches"] == ["BA-003"]
            and trace["metric_version"]["method_version"] == "GC-2026"
            and trace["metric_version"]["adjustment"]["method_offset"] == -0.5
            and {item["batch_id"] for item in trace["evidence"]["measurements"]}
            == {"BA-001", "BA-002", "BA-003"})

        # 用途分离：排名数据不能作为放行依据，放行权限与排名权限互相独立
        try:
            service.trace_score(actor_id="exec-1", cycle_id="cycle-2026q3", site_id="site-a",
                                metric_id="methanol", purpose="batch_release")
            checks["ranking_not_usable_for_release"] = False
        except PermissionDenied:
            checks["ranking_not_usable_for_release"] = True
        try:
            service.record_release_decision(request_id="rel-denied", actor_id="exec-1",
                                            site_id="site-a", batch_id="BA-001",
                                            decision="released", basis={"document": "QA-09"})
            checks["ranking_role_cannot_release"] = False
        except PermissionDenied:
            checks["ranking_role_cannot_release"] = True
        try:
            service.get_ranking(actor_id="qa-release", cycle_id="cycle-2026q3")
            checks["release_role_cannot_view_ranking"] = False
        except PermissionDenied:
            checks["release_role_cannot_view_ranking"] = True
        service.record_release_decision(request_id="rel-1", actor_id="qa-release",
                                        site_id="site-a", batch_id="BA-001", decision="released",
                                        basis={"inspection_report": "QA-2026-101",
                                               "standard": "GB-2757"})

        valid, event_count = service.verify_audit()
        checks["audit_valid"] = valid
        result: dict[str, object] = {"status": "ok" if all(checks.values()) else "failed",
                                     "checks": checks, "audit_events": event_count,
                                     "audit_valid": valid}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
