"""跨工厂质量对标平台的离线端到端验收。

在临时 SQLite 中走完整条链路：
指标版本化 → 周期锁定 → 证据填报 → 异常值独立审查（留存名次影响）
→ 冻结 → 争议指标暂停、其余发布 → 裁定更正后重新发布（旧版不可变）
→ 发现/整改/复验（不改写排名）→ 批次放行权限分离 → 分数追溯与审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .benchmark import BenchmarkService
from .clock import FixedClock
from .errors import PermissionDenied
from .storage import Database


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "benchmark_acceptance.sqlite3")
        service = BenchmarkService(database, FixedClock(datetime(2026, 10, 2, tzinfo=timezone.utc)))

        # 主体、角色与工厂（o2 为独立审查机构）
        service.register_organization(request_id="org1", actor_id="bootstrap",
                                      organization_id="o1", name="酒业集团")
        service.register_organization(request_id="org2", actor_id="bootstrap",
                                      organization_id="o2", name="独立质量研究院")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                               display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role, org in [
            ("qm", "qm1", "质量总监", "quality_manager", "o1"),
            ("op1", "op1", "一厂操作员", "operator", "o1"),
            ("op2", "op2", "二厂操作员", "operator", "o1"),
            ("rev", "rev1", "独立审查员", "reviewer", "o2"),
            ("rel", "rel1", "批次放行人", "release_officer", "o1"),
        ]:
            service.register_actor(request_id=rid, actor_id="ad1", new_actor_id=aid,
                                   display_name=name, role=role, organization_id=org)
        service.register_site(request_id="s1", actor_id="ad1", site_id="site-1",
                              organization_id="o1", name="一厂", timezone_name="Asia/Shanghai")
        service.register_site(request_id="s2", actor_id="ad1", site_id="site-2",
                              organization_id="o1", name="二厂", timezone_name="Asia/Shanghai")

        # 指标两版：方法 2021 -> 2025；周期锁定 v1
        service.define_metric(request_id="mp1", actor_id="qm1", metric_id="purity", code="PURITY",
                              name="纯度合格率", direction="higher_better", weight=0.6,
                              applicable_products=["liquor-a"],
                              sampling={"basis": "batch", "window": "cycle"},
                              method_code="GB-P", method_version="2021",
                              rule={"type": "threshold_rate", "lower": 90, "upper": 100})
        service.define_metric(request_id="mp2", actor_id="qm1", metric_id="purity", code="PURITY",
                              name="纯度合格率", direction="higher_better", weight=0.6,
                              applicable_products=["liquor-a", "liquor-b"],
                              sampling={"basis": "batch", "window": "cycle"},
                              method_code="GB-P", method_version="2025",
                              rule={"type": "threshold_rate", "lower": 92, "upper": 100})
        service.define_metric(request_id="mf1", actor_id="qm1", metric_id="fault", code="FAULT",
                              name="缺陷率", direction="lower_better", weight=0.4,
                              applicable_products=["liquor-a"], sampling={"basis": "batch"},
                              method_code="GB-F", method_version="1.0",
                              rule={"type": "mean", "scale": 100})
        service.create_cycle(request_id="cyc", actor_id="qm1", cycle_id="cyc-1", code="2026Q3",
                             period_start="2026-07-01", period_end="2026-09-30")
        service.add_cycle_metric(request_id="cmp", actor_id="qm1", cycle_id="cyc-1",
                                 metric_id="purity", metric_version=1)
        service.add_cycle_metric(request_id="cmf", actor_id="qm1", cycle_id="cyc-1", metric_id="fault")
        service.add_cycle_site(request_id="cs1", actor_id="qm1", cycle_id="cyc-1", site_id="site-1")
        service.add_cycle_site(request_id="cs2", actor_id="qm1", cycle_id="cyc-1", site_id="site-2")
        # 二厂新灌装线的可比性调整
        service.set_adjustment(request_id="adj", actor_id="qm1", cycle_id="cyc-1",
                               site_id="site-2", metric_id="fault", factor=0.95,
                               reason="产线改造磨合期可比性调整", evidence={"doc": "line-change-7"})

        def evidence(site, actor, values):
            for index, (metric, value) in enumerate(values):
                service.submit_evidence(
                    request_id=f"ev-{site}-{index}", actor_id=actor, cycle_id="cyc-1",
                    site_id=site, metric_id=metric, batch_no=f"{site}-{metric}-{index}",
                    product_code="liquor-a", sampled_at="2026-08-01T00:00:00Z",
                    raw_value=value, payload={"pass": value >= 90} if metric == "purity" else {})

        evidence("site-1", "op1", [("purity", 95), ("purity", 96), ("purity", 94), ("purity", 20),
                                   ("fault", 10), ("fault", 12), ("fault", 11), ("fault", 13)])
        evidence("site-2", "op2", [("purity", 95), ("purity", 93), ("purity", 94), ("purity", 96),
                                   ("fault", 20), ("fault", 8), ("fault", 9), ("fault", 7)])

        # 异常值排除：独立人员审查，留存排除前后名次影响
        outlier = database.connection.execute(
            "SELECT evidence_id FROM metric_evidence WHERE site_id='site-1' AND metric_id='purity' "
            "AND raw_value=20").fetchone()["evidence_id"]
        exclusion = service.request_exclusion(
            request_id="ex1", actor_id="op1", cycle_id="cyc-1", evidence_id=outlier,
            reason="仪器故障，当日复测合格")
        reviewed = service.review_exclusion(
            request_id="rx1", actor_id="rev1", exclusion_id=exclusion["exclusion_id"],
            decision="approved", review_note="证据链完整，同意排除")
        metric_impact = next(item for item in reviewed["impact"] if item["scope"] == "metric")

        # 冻结
        frozen = service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        ranking = service.frozen_ranking("cyc-1")

        # 二厂对缺陷率提异议：v1 只暂停该格，其他照常发布
        service.raise_dispute(request_id="d1", actor_id="op2", cycle_id="cyc-1",
                              site_id="site-2", metric_id="fault", reason="采样窗口跨产线改造")
        v1 = service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1", note="首次发布")
        v1_rows = {(r["site_id"], r["metric_id"]): r for r in service.get_publication("cyc-1", 1)["rows"]}

        # 裁定更正并发布 v2；v1 快照保持不变
        dispute_id = database.connection.execute(
            "SELECT dispute_id FROM metric_disputes").fetchone()["dispute_id"]
        service.resolve_dispute(request_id="rd1", actor_id="qm1", dispute_id=dispute_id,
                                resolution="corrected", corrected_score=88.0, note="按调整口径更正")
        service.publish_cycle(request_id="p2", actor_id="qm1", cycle_id="cyc-1", note="更正后发布")
        v2_rows = {(r["site_id"], r["metric_id"]): r for r in service.get_publication("cyc-1", 2)["rows"]}

        # 发现 → 整改计划 → 复验；通过后排名不得被改写
        service.create_finding(request_id="f1", actor_id="rev1", cycle_id="cyc-1", site_id="site-1",
                               source="ranking", severity="medium", metric_id="purity",
                               description="异常批次暴露仪器维护缺口")
        finding_id = database.connection.execute("SELECT finding_id FROM findings").fetchone()["finding_id"]
        service.create_rectification_plan(request_id="pl1", actor_id="qm1", finding_id=finding_id,
                                          owner_actor_id="op1", due_date="2026-11-30")
        plan_id = database.connection.execute(
            "SELECT plan_id FROM rectification_plans").fetchone()["plan_id"]
        service.submit_verification(request_id="vf1", actor_id="op1", plan_id=plan_id,
                                    evidence={"maintenance": "done", "retest_batches": 3})
        verification_id = database.connection.execute(
            "SELECT verification_id FROM rectification_verifications").fetchone()["verification_id"]
        service.review_verification(request_id="vr1", actor_id="rev1", verification_id=verification_id,
                                    accepted=True, review_note="复验通过")
        plan_after = service.get_plan(plan_id)["plan"]

        # 批次放行：质量角色被拒，放行人可决定
        try:
            service.decide_batch_release(request_id="br-bad", actor_id="qm1", site_id="site-1",
                                         batch_no="B-009", product_code="liquor-a",
                                         decision="released", reason="试图放行")
            release_denied = False
        except PermissionDenied:
            release_denied = True
        release = service.decide_batch_release(
            request_id="br1", actor_id="rel1", site_id="site-1", batch_no="B-009",
            product_code="liquor-a", decision="released", reason="检验合格")

        # 分数追溯
        trace = service.trace_score("cyc-1", "site-1", "purity")
        audit_valid, audit_events = service.verify_audit()

        checks = {
            "metric_pinned_version": ranking["metric_versions"]["purity"] == 1,
            "exclusion_independent": metric_impact["rank_before"] == 2
            and metric_impact["rank_after"] == 1,
            "frozen_status": frozen["status"] == "frozen",
            "v1_held_only_disputed_cell": v1_rows[("site-2", "fault")]["state"] == "held_disputed"
            and v1_rows[("site-2", "purity")]["state"] == "published"
            and v1_rows[("site-1", "fault")]["state"] == "published",
            "v1_immutable": v1_rows[("site-2", "fault")]["score"] is None,
            "v2_corrected": v2_rows[("site-2", "fault")]["score"] == 88.0,
            "plan_closed": plan_after["status"] == "closed",
            "ranking_unchanged_after_rectification":
                service.get_publication("cyc-1", 2)["manifest"]["scores"]["site-1"]["purity"]
                == ranking["cells"]["site-1"]["purity"],
            "release_permission_separated": release_denied and release["decision"] == "released",
            "trace_has_batches_rule_hash": len(trace["evidence"]) == 4
            and trace["frozen"]["calc"]["n"] == 3
            and trace["frozen"]["calc"]["excluded_evidence"]
            and trace["metric_definition"]["method_version"] == "2021",
            "audit_valid": audit_valid,
        }
        result = {"status": "ok" if all(checks.values()) else "failed",
                  "checks": checks, "audit_events": audit_events,
                  "total_rank_after_exclusion": ranking["total_ranks"]["site-1"]}
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
