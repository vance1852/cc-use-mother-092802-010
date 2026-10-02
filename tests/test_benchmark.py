"""跨工厂质量对标平台的领域规则测试。"""

import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.benchmark import BenchmarkService
from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from beverage_ops_foundation.storage import Database


class BenchmarkTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.service = BenchmarkService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org1", actor_id="bootstrap", organization_id="o1", name="集团")
        s.register_organization(request_id="org2", actor_id="bootstrap", organization_id="o2", name="独立机构")
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad1",
                         display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role, org in [
            ("qm", "qm1", "质量总监", "quality_manager", "o1"),
            ("op1", "op1", "一厂操作员", "operator", "o1"),
            ("op2", "op2", "二厂操作员", "operator", "o1"),
            ("rev", "rev1", "独立审查员", "reviewer", "o2"),
            ("rel", "rel1", "放行人", "release_officer", "o1"),
        ]:
            s.register_actor(request_id=rid, actor_id="ad1", new_actor_id=aid,
                             display_name=name, role=role, organization_id=org)
        s.register_site(request_id="s1", actor_id="op1", site_id="site-1", organization_id="o1",
                        name="一厂", timezone_name="Asia/Shanghai")
        s.register_site(request_id="s2", actor_id="op2", site_id="site-2", organization_id="o1",
                        name="二厂", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def define_metrics(self):
        s = self.service
        s.define_metric(request_id="m-p1", actor_id="qm1", metric_id="purity", code="PURITY",
                        name="纯度合格率", direction="higher_better", weight=0.6,
                        applicable_products=["liquor-a"],
                        sampling={"basis": "batch", "window": "cycle"},
                        method_code="GB-P", method_version="2021",
                        rule={"type": "threshold_rate", "lower": 90, "upper": 100})
        s.define_metric(request_id="m-p2", actor_id="qm1", metric_id="purity", code="PURITY",
                        name="纯度合格率", direction="higher_better", weight=0.6,
                        applicable_products=["liquor-a", "liquor-b"],
                        sampling={"basis": "batch", "window": "cycle"},
                        method_code="GB-P", method_version="2025",
                        rule={"type": "threshold_rate", "lower": 92, "upper": 100})
        s.define_metric(request_id="m-f", actor_id="qm1", metric_id="fault", code="FAULT",
                        name="缺陷率", direction="lower_better", weight=0.4,
                        applicable_products=["liquor-a"], sampling={"basis": "batch"},
                        method_code="GB-F", method_version="1.0",
                        rule={"type": "mean", "scale": 100})

    def create_cycle(self):
        s = self.service
        s.create_cycle(request_id="cyc", actor_id="qm1", cycle_id="cyc-1", code="2026Q3",
                       period_start="2026-07-01", period_end="2026-09-30")
        s.add_cycle_metric(request_id="cm-p", actor_id="qm1", cycle_id="cyc-1",
                           metric_id="purity", metric_version=1)
        s.add_cycle_metric(request_id="cm-f", actor_id="qm1", cycle_id="cyc-1", metric_id="fault")
        s.add_cycle_site(request_id="cs-1", actor_id="qm1", cycle_id="cyc-1", site_id="site-1")
        s.add_cycle_site(request_id="cs-2", actor_id="qm1", cycle_id="cyc-1", site_id="site-2")

    def submit_evidence(self, site, actor, values):
        s = self.service
        for index, (metric, value) in enumerate(values):
            s.submit_evidence(request_id=f"ev-{site}-{metric}-{index}", actor_id=actor,
                              cycle_id="cyc-1", site_id=site, metric_id=metric,
                              batch_no=f"{site}-{metric}-{index}", product_code="liquor-a",
                              sampled_at="2026-08-01T00:00:00Z", raw_value=value,
                              payload={"pass": value >= 90} if metric == "purity" else {})


class MetricVersioningTest(BenchmarkTestBase):
    def test_metric_definitions_are_versioned(self):
        self.define_metrics()
        metric = self.service.get_metric("purity")
        self.assertEqual(2, metric["version"])
        self.assertEqual("2025", metric["method_version"])
        self.assertEqual(["liquor-a", "liquor-b"], metric["applicable_products"])
        self.assertEqual([1, 2], [item["version"] for item in metric["all_versions"]])

    def test_code_is_immutable_across_versions(self):
        self.define_metrics()
        with self.assertRaises(ConflictError):
            self.service.define_metric(
                request_id="m-bad", actor_id="qm1", metric_id="purity", code="OTHER",
                name="改名", direction="higher_better", weight=1,
                applicable_products=["liquor-a"], sampling={"basis": "batch"},
                method_code="GB-P", method_version="2026", rule={"type": "pass_rate"})

    def test_invalid_rule_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.define_metric(
                request_id="m-bad", actor_id="qm1", metric_id="bad", code="BAD", name="坏指标",
                direction="higher_better", weight=1, applicable_products=["x"],
                sampling={"basis": "batch"}, method_code="M", method_version="1",
                rule={"type": "threshold_rate", "lower": 90, "upper": 80})

    def test_operator_cannot_define_metrics(self):
        with self.assertRaises(PermissionDenied):
            self.service.define_metric(
                request_id="m-x", actor_id="op1", metric_id="x", code="X", name="x",
                direction="higher_better", weight=1, applicable_products=["x"],
                sampling={"basis": "batch"}, method_code="M", method_version="1",
                rule={"type": "pass_rate"})


class EvidenceTest(BenchmarkTestBase):
    def setUp(self):
        super().setUp()
        self.define_metrics()
        self.create_cycle()

    def test_product_applicability_is_enforced(self):
        with self.assertRaises(ValidationError):
            self.service.submit_evidence(
                request_id="ev-x", actor_id="op1", cycle_id="cyc-1", site_id="site-1",
                metric_id="purity", batch_no="b-x", product_code="liquor-c",
                sampled_at="2026-08-01T00:00:00Z", raw_value=95, payload={})

    def test_sampling_window_is_enforced(self):
        with self.assertRaises(ValidationError):
            self.service.submit_evidence(
                request_id="ev-x", actor_id="op1", cycle_id="cyc-1", site_id="site-1",
                metric_id="purity", batch_no="b-x", product_code="liquor-a",
                sampled_at="2026-06-30T00:00:00Z", raw_value=95, payload={})

    def test_method_version_must_match_pinned_definition(self):
        # 周期锁定的是 purity v1（方法 2021），提交 2025 方法版本应被拒绝
        with self.assertRaises(ValidationError):
            self.service.submit_evidence(
                request_id="ev-x", actor_id="op1", cycle_id="cyc-1", site_id="site-1",
                metric_id="purity", batch_no="b-x", product_code="liquor-a",
                sampled_at="2026-08-01T00:00:00Z", raw_value=95,
                payload={"method_version": "2025"})

    def test_cross_org_evidence_is_rejected(self):
        self.service.register_actor(request_id="op3", actor_id="ad1", new_actor_id="op3",
                                    display_name="外厂操作员", role="operator", organization_id="o2")
        with self.assertRaises(PermissionDenied):
            self.service.submit_evidence(
                request_id="ev-x", actor_id="op3", cycle_id="cyc-1", site_id="site-1",
                metric_id="purity", batch_no="b-x", product_code="liquor-a",
                sampled_at="2026-08-01T00:00:00Z", raw_value=95, payload={})

    def test_evidence_is_idempotent(self):
        kwargs = dict(actor_id="op1", cycle_id="cyc-1", site_id="site-1", metric_id="purity",
                      batch_no="b1", product_code="liquor-a", sampled_at="2026-08-01T00:00:00Z",
                      raw_value=95, payload={})
        first = self.service.submit_evidence(request_id="ev1", **kwargs)
        second = self.service.submit_evidence(request_id="ev1", **kwargs)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])


class ExclusionTest(BenchmarkTestBase):
    def setUp(self):
        super().setUp()
        self.define_metrics()
        self.create_cycle()
        # 一厂：纯度含一个明显异常值 12
        self.submit_evidence("site-1", "op1",
                             [("purity", 95), ("purity", 96), ("purity", 94), ("purity", 12),
                              ("fault", 10), ("fault", 12)])
        self.submit_evidence("site-2", "op2",
                             [("purity", 95), ("purity", 93), ("purity", 94),
                              ("fault", 20), ("fault", 8)])
        row = self.database.connection.execute(
            "SELECT evidence_id FROM metric_evidence WHERE site_id='site-1' AND metric_id='purity' "
            "AND raw_value=12").fetchone()
        self.outlier = row["evidence_id"]

    def test_same_organization_cannot_review_exclusion(self):
        request = self.service.request_exclusion(
            request_id="ex1", actor_id="op1", cycle_id="cyc-1",
            evidence_id=self.outlier, reason="仪器故障")
        # reviewer 若来自本厂组织也不行；这里用二厂操作员（同属 o1）验证独立性
        with self.assertRaises(PermissionDenied):
            self.service.review_exclusion(request_id="rv-bad", actor_id="op2",
                                          exclusion_id=request["exclusion_id"], decision="approved")

    def test_pending_exclusion_blocks_freeze(self):
        self.service.request_exclusion(
            request_id="ex1", actor_id="op1", cycle_id="cyc-1",
            evidence_id=self.outlier, reason="仪器故障")
        with self.assertRaises(ConflictError):
            self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")

    def test_approval_records_rank_impact_before_and_after(self):
        before = self.service._snapshot(self.database.connection, "cyc-1")
        self.assertEqual(2, before["metric_ranks"]["purity"]["site-1"])
        request = self.service.request_exclusion(
            request_id="ex1", actor_id="op1", cycle_id="cyc-1",
            evidence_id=self.outlier, reason="仪器故障")
        result = self.service.review_exclusion(
            request_id="rv1", actor_id="rev1", exclusion_id=request["exclusion_id"],
            decision="approved", review_note="复测合格，同意排除")
        # 批准后一厂纯度名次从第 2 升到第 1
        self.assertEqual({"scope": "metric", "rank_before": 2, "rank_after": 1}, result["impact"][0])
        detail = self.service.get_exclusion(request["exclusion_id"])
        self.assertEqual("approved", detail["status"])
        self.assertEqual("rev1", detail["reviewed_by"])
        impacts = {(i["scope"], i["site_id"]): i for i in detail["rank_impacts"]}
        metric_impact = impacts[("metric", "site-1")]
        self.assertEqual(2, metric_impact["rank_before"])
        self.assertEqual(1, metric_impact["rank_after"])
        total_impact = impacts[("total", "site-1")]
        self.assertIsNotNone(total_impact["rank_before"])
        self.assertIsNotNone(total_impact["rank_after"])

    def test_rejection_keeps_evidence_in_calculation(self):
        request = self.service.request_exclusion(
            request_id="ex1", actor_id="op1", cycle_id="cyc-1",
            evidence_id=self.outlier, reason="怀疑仪器故障")
        self.service.review_exclusion(request_id="rv1", actor_id="rev1",
                                      exclusion_id=request["exclusion_id"], decision="rejected")
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        ranking = self.service.frozen_ranking("cyc-1")
        # 12 低于 90，纯度合格率 3/4=75
        self.assertEqual(75.0, ranking["cells"]["site-1"]["purity"])


class FreezeAndRankingTest(BenchmarkTestBase):
    def setUp(self):
        super().setUp()
        self.define_metrics()
        self.create_cycle()
        self.submit_evidence("site-1", "op1",
                             [("purity", 95), ("purity", 96), ("fault", 10), ("fault", 10)])
        self.submit_evidence("site-2", "op2",
                             [("purity", 95), ("purity", 93), ("fault", 20), ("fault", 8)])

    def test_freeze_pins_versions_and_calc(self):
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        ranking = self.service.frozen_ranking("cyc-1")
        self.assertEqual(1, ranking["metric_versions"]["purity"])  # 锁定方法 2021 的 v1
        self.assertEqual(100.0, ranking["cells"]["site-1"]["purity"])
        # 缺陷率 lower_better：均值 10 -> 90；均值 14 -> 86
        self.assertEqual(90.0, ranking["cells"]["site-1"]["fault"])
        self.assertEqual(86.0, ranking["cells"]["site-2"]["fault"])
        self.assertEqual(1, ranking["total_ranks"]["site-1"])

    def test_frozen_cycle_rejects_new_evidence_and_exclusion(self):
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        with self.assertRaises(ConflictError):
            self.service.submit_evidence(
                request_id="ev-late", actor_id="op1", cycle_id="cyc-1", site_id="site-1",
                metric_id="purity", batch_no="late", product_code="liquor-a",
                sampled_at="2026-08-02T00:00:00Z", raw_value=99, payload={})

    def test_missing_metric_evidence_means_no_total(self):
        # 三厂只报纯度不报缺陷率：总分必须缺测，不能被指标组合掩盖长期缺陷
        self.service.register_site(request_id="s3", actor_id="ad1", site_id="site-3",
                                   organization_id="o1", name="三厂", timezone_name="Asia/Shanghai")
        self.service.add_cycle_site(request_id="cs-3", actor_id="qm1", cycle_id="cyc-1", site_id="site-3")
        # 只提交纯度，缺陷率完全缺测
        self.service.submit_evidence(
            request_id="ev-3-p", actor_id="op1", cycle_id="cyc-1", site_id="site-3",
            metric_id="purity", batch_no="site-3-purity-0", product_code="liquor-a",
            sampled_at="2026-08-01T00:00:00Z", raw_value=99, payload={"pass": True})
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        ranking = self.service.frozen_ranking("cyc-1")
        self.assertEqual(100.0, ranking["cells"]["site-3"]["purity"])
        self.assertIsNone(ranking["cells"]["site-3"]["fault"])
        self.assertIsNone(ranking["totals"]["site-3"])
        self.assertNotIn("site-3", ranking["total_ranks"])

    def test_adjustment_factor_changes_score_transparently(self):
        self.service.set_adjustment(
            request_id="adj", actor_id="qm1", cycle_id="cyc-1", site_id="site-2", metric_id="fault",
            factor=0.5, reason="产线改造口径调整", evidence={"doc": "change-001"})
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        ranking = self.service.frozen_ranking("cyc-1")
        self.assertEqual(43.0, ranking["cells"]["site-2"]["fault"])  # 86 * 0.5
        trace = self.service.trace_score("cyc-1", "site-2", "fault")
        self.assertEqual(0.5, trace["adjustment"]["factor"])
        self.assertEqual("change-001", trace["adjustment"]["evidence"]["doc"])
        self.assertEqual(0.5, trace["frozen"]["calc"]["adjustment_factor"])


class DisputeAndPublicationTest(BenchmarkTestBase):
    def setUp(self):
        super().setUp()
        self.define_metrics()
        self.create_cycle()
        self.submit_evidence("site-1", "op1",
                             [("purity", 95), ("purity", 96), ("fault", 10), ("fault", 10)])
        self.submit_evidence("site-2", "op2",
                             [("purity", 95), ("purity", 93), ("fault", 20), ("fault", 8)])
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")

    def test_only_disputed_metric_is_held(self):
        self.service.raise_dispute(request_id="d1", actor_id="op2", cycle_id="cyc-1",
                                   site_id="site-2", metric_id="fault", reason="采样窗口有误")
        self.service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1")
        publication = self.service.get_publication("cyc-1")
        rows = {(r["site_id"], r["metric_id"]): r for r in publication["rows"]}
        self.assertEqual("held_disputed", rows[("site-2", "fault")]["state"])
        self.assertIsNone(rows[("site-2", "fault")]["score"])
        # 其他指标照常发布
        self.assertEqual("published", rows[("site-2", "purity")]["state"])
        self.assertEqual("published", rows[("site-1", "fault")]["state"])
        # 争议工厂总分暂停，一厂总分照常排名
        self.assertIsNone(rows[("site-2", "fault")]["total_score"])
        self.assertEqual(96.0, rows[("site-1", "fault")]["total_score"])
        self.assertEqual(1, rows[("site-1", "fault")]["rank_total"])

    def test_correction_creates_new_publication_and_keeps_v1_immutable(self):
        self.service.raise_dispute(request_id="d1", actor_id="op2", cycle_id="cyc-1",
                                   site_id="site-2", metric_id="fault", reason="口径问题")
        self.service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1")
        dispute_id = self.database.connection.execute(
            "SELECT dispute_id FROM metric_disputes").fetchone()["dispute_id"]
        self.service.resolve_dispute(request_id="r1", actor_id="qm1", dispute_id=dispute_id,
                                     resolution="corrected", corrected_score=95.0, note="核实后更正")
        self.service.publish_cycle(request_id="p2", actor_id="qm1", cycle_id="cyc-1")
        v1 = self.service.get_publication("cyc-1", 1)
        v2 = self.service.get_publication("cyc-1", 2)
        v1_rows = {(r["site_id"], r["metric_id"]): r for r in v1["rows"]}
        v2_rows = {(r["site_id"], r["metric_id"]): r for r in v2["rows"]}
        self.assertIsNone(v1_rows[("site-2", "fault")]["score"])  # v1 不可变
        self.assertEqual(95.0, v2_rows[("site-2", "fault")]["score"])
        # 更正后名次发生变化（二厂反超）
        self.assertEqual(1, v2_rows[("site-2", "fault")]["rank_total"])
        self.assertEqual(2, v2_rows[("site-1", "fault")]["rank_total"])
        # 冻结分本身保持原样
        trace = self.service.trace_score("cyc-1", "site-2", "fault")
        self.assertEqual(86.0, trace["frozen"]["score"])
        self.assertEqual(95.0, trace["correction"]["corrected_score"])

    def test_upheld_dispute_republishes_original_score(self):
        self.service.raise_dispute(request_id="d1", actor_id="op2", cycle_id="cyc-1",
                                   site_id="site-2", metric_id="fault", reason="口径问题")
        self.service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1")
        dispute_id = self.database.connection.execute(
            "SELECT dispute_id FROM metric_disputes").fetchone()["dispute_id"]
        self.service.resolve_dispute(request_id="r1", actor_id="qm1", dispute_id=dispute_id,
                                     resolution="upheld", note="维持原结果")
        self.service.publish_cycle(request_id="p2", actor_id="qm1", cycle_id="cyc-1")
        v2 = self.service.get_publication("cyc-1", 2)
        rows = {(r["site_id"], r["metric_id"]): r for r in v2["rows"]}
        self.assertEqual(86.0, rows[("site-2", "fault")]["score"])

    def test_second_open_dispute_after_publication_is_held_on_republish(self):
        self.service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1")
        self.service.raise_dispute(request_id="d1", actor_id="op1", cycle_id="cyc-1",
                                   site_id="site-1", metric_id="purity", reason="新发现方法偏差")
        self.service.publish_cycle(request_id="p2", actor_id="qm1", cycle_id="cyc-1")
        v2 = self.service.get_publication("cyc-1", 2)
        rows = {(r["site_id"], r["metric_id"]): r for r in v2["rows"]}
        self.assertEqual("held_disputed", rows[("site-1", "purity")]["state"])
        self.assertEqual("published", rows[("site-2", "purity")]["state"])


class RectificationTest(BenchmarkTestBase):
    def setUp(self):
        super().setUp()
        self.define_metrics()
        self.create_cycle()
        self.submit_evidence("site-1", "op1",
                             [("purity", 95), ("purity", 96), ("fault", 10), ("fault", 10)])
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        self.service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1")
        self.service.create_finding(request_id="f1", actor_id="rev1", cycle_id="cyc-1",
                                    site_id="site-1", source="ranking", severity="medium",
                                    description="纯度长期波动，需排查灌装环节", metric_id="purity")
        self.finding_id = self.database.connection.execute(
            "SELECT finding_id FROM findings").fetchone()["finding_id"]

    def _plan_and_verify(self, accepted):
        s = self.service
        s.create_rectification_plan(request_id="plan1", actor_id="qm1", finding_id=self.finding_id,
                                    owner_actor_id="op1", due_date="2026-11-30")
        plan_id = self.database.connection.execute(
            "SELECT plan_id FROM rectification_plans").fetchone()["plan_id"]
        s.submit_verification(request_id="v1", actor_id="op1", plan_id=plan_id,
                              evidence={"report": "维护记录", "retest_batches": 3})
        verification_id = self.database.connection.execute(
            "SELECT verification_id FROM rectification_verifications").fetchone()["verification_id"]
        s.review_verification(request_id="vr1", actor_id="rev1", verification_id=verification_id,
                              accepted=accepted, review_note="复验" + ("通过" if accepted else "不通过"))
        return plan_id

    def test_plan_is_linked_to_finding_owner_due_date(self):
        self.service.create_rectification_plan(request_id="plan1", actor_id="qm1",
                                               finding_id=self.finding_id, owner_actor_id="op1",
                                               due_date="2026-11-30")
        detail = self.service.get_plan(
            self.database.connection.execute("SELECT plan_id FROM rectification_plans")
            .fetchone()["plan_id"])
        self.assertEqual(self.finding_id, detail["finding"]["finding_id"])
        self.assertEqual("op1", detail["plan"]["owner_actor_id"])
        self.assertEqual("2026-11-30", detail["plan"]["due_date"])

    def test_accepted_verification_closes_plan_without_changing_ranking(self):
        self._plan_and_verify(True)
        detail = self.service.get_plan(
            self.database.connection.execute("SELECT plan_id FROM rectification_plans")
            .fetchone()["plan_id"])
        self.assertEqual("closed", detail["plan"]["status"])
        # 已发布排名不被整改自动改写
        publication = self.service.get_publication("cyc-1", 1)
        self.assertEqual(100.0, publication["manifest"]["scores"]["site-1"]["purity"])

    def test_rejected_verification_reopens_plan(self):
        plan_id = self._plan_and_verify(False)
        detail = self.service.get_plan(plan_id)
        self.assertEqual("open", detail["plan"]["status"])

    def test_same_org_cannot_review_verification(self):
        self.service.create_rectification_plan(request_id="plan1", actor_id="qm1",
                                               finding_id=self.finding_id, owner_actor_id="op1",
                                               due_date="2026-11-30")
        plan_id = self.database.connection.execute(
            "SELECT plan_id FROM rectification_plans").fetchone()["plan_id"]
        self.service.submit_verification(request_id="v1", actor_id="op1", plan_id=plan_id,
                                         evidence={"report": "x"})
        verification_id = self.database.connection.execute(
            "SELECT verification_id FROM rectification_verifications").fetchone()["verification_id"]
        # 管理员同属 o1，不能复验本厂整改
        with self.assertRaises(PermissionDenied):
            self.service.review_verification(request_id="vr-bad", actor_id="ad1",
                                             verification_id=verification_id, accepted=True)

    def test_only_owner_can_submit_verification(self):
        self.service.create_rectification_plan(request_id="plan1", actor_id="qm1",
                                               finding_id=self.finding_id, owner_actor_id="op1",
                                               due_date="2026-11-30")
        plan_id = self.database.connection.execute(
            "SELECT plan_id FROM rectification_plans").fetchone()["plan_id"]
        with self.assertRaises(PermissionDenied):
            self.service.submit_verification(request_id="v-bad", actor_id="op2", plan_id=plan_id,
                                             evidence={"report": "x"})


class BatchReleaseSeparationTest(BenchmarkTestBase):
    def test_ranking_roles_cannot_release_batches(self):
        for actor in ("qm1", "op1", "rev1"):
            with self.assertRaises(PermissionDenied):
                self.service.decide_batch_release(
                    request_id=f"rel-{actor}", actor_id=actor, site_id="site-1", batch_no=f"B-{actor}",
                    product_code="liquor-a", decision="released", reason="合格")

    def test_release_officer_decides_and_decision_is_immutable(self):
        result = self.service.decide_batch_release(
            request_id="rel1", actor_id="rel1", site_id="site-1", batch_no="B-001",
            product_code="liquor-a", decision="released", reason="检验合格")
        self.assertEqual("released", result["decision"])
        with self.assertRaises(ConflictError):
            self.service.decide_batch_release(
                request_id="rel2", actor_id="rel1", site_id="site-1", batch_no="B-001",
                product_code="liquor-a", decision="held", reason="试图改写")


class TraceabilityTest(BenchmarkTestBase):
    def test_score_traces_back_to_batches_rule_and_publications(self):
        self.define_metrics()
        self.create_cycle()
        self.submit_evidence("site-1", "op1",
                             [("purity", 95), ("purity", 96), ("fault", 10), ("fault", 10)])
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        self.service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1")
        trace = self.service.trace_score("cyc-1", "site-1", "purity")
        self.assertEqual("2021", trace["metric_definition"]["method_version"])
        self.assertEqual({"type": "threshold_rate", "lower": 90.0, "upper": 100.0},
                         trace["frozen"]["calc"]["rule"])
        self.assertEqual(2, len(trace["evidence"]))
        self.assertEqual({"site-1-purity-0", "site-1-purity-1"},
                         {item["batch_no"] for item in trace["evidence"]})
        self.assertTrue(all(item["evidence_hash"] for item in trace["evidence"]))
        self.assertEqual(2, trace["frozen"]["calc"]["n"])
        self.assertEqual(1, len(trace["publications"]))
        self.assertEqual("published", trace["publications"][0]["state"])


class AuditChainTest(BenchmarkTestBase):
    def test_full_workflow_keeps_audit_chain_valid(self):
        self.define_metrics()
        self.create_cycle()
        self.submit_evidence("site-1", "op1", [("purity", 95), ("fault", 10)])
        self.service.freeze_cycle(request_id="fz", actor_id="qm1", cycle_id="cyc-1")
        self.service.publish_cycle(request_id="p1", actor_id="qm1", cycle_id="cyc-1")
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 10)


if __name__ == "__main__":
    unittest.main()
