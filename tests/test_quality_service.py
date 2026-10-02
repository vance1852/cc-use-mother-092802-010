import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError

from quality_benchmark.service import QualityService
from quality_benchmark.storage import QualityDatabase


METHANOL_V1 = dict(metric_id="methanol", name="甲醇含量", unit="mg/L", direction="lower_better",
                   weight=0.6, applicable_products=["baijiu"],
                   sampling_window={"start": "2026-08-01", "end": "2026-09-30"},
                   method_version="GC-2015",
                   adjustment={"product_coefficients": {"baijiu": 1.0}, "method_offset": 0.0})
METHANOL_V2 = dict(METHANOL_V1, method_version="GC-2026",
                   adjustment={"product_coefficients": {"baijiu": 1.0}, "method_offset": -0.5})
SENSORY_V1 = dict(metric_id="sensory", name="感官评分", unit="分", direction="higher_better",
                  weight=0.4, applicable_products=["baijiu", "fruit_wine"],
                  sampling_window={"start": "2026-08-01", "end": "2026-09-30"},
                  method_version="SENS-01",
                  adjustment={"product_coefficients": {"baijiu": 1.0, "fruit_wine": 0.98}})


class QualityServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = QualityDatabase()
        self.service = QualityService(
            self.database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org-hq", actor_id="bootstrap",
                                           organization_id="hq", name="集团质量中心")
        self.service.register_actor(request_id="actor-admin", actor_id="bootstrap",
                                    new_actor_id="admin", display_name="质量管理员",
                                    role="admin", organization_id="hq")
        self.service.register_organization(request_id="org-fa", actor_id="admin",
                                           organization_id="fa", name="甲酒厂")
        self.service.register_organization(request_id="org-fb", actor_id="admin",
                                           organization_id="fb", name="乙酒厂")
        for request_id, actor_id, name, role, org in [
                ("actor-reviewer", "reviewer", "独立审查人", "reviewer", "hq"),
                ("actor-reviewer-fa", "reviewer-fa", "甲厂审查人", "reviewer", "fa"),
                ("actor-exec", "exec", "管理层", "auditor", "hq"),
                ("actor-release", "releaser", "放行负责人", "operator", "hq"),
                ("actor-opa", "op-a", "甲厂质量员", "operator", "fa"),
                ("actor-opb", "op-b", "乙厂质量员", "operator", "fb")]:
            self.service.register_actor(request_id=request_id, actor_id="admin",
                                        new_actor_id=actor_id, display_name=name,
                                        role=role, organization_id=org)
        self.service.register_site(request_id="site-a", actor_id="admin", site_id="site-a",
                                   organization_id="fa", name="甲厂基地", timezone_name="Asia/Shanghai")
        self.service.register_site(request_id="site-b", actor_id="admin", site_id="site-b",
                                   organization_id="fb", name="乙厂基地", timezone_name="Asia/Shanghai")
        self.methanol_v1 = self._define("met-v1", METHANOL_V1)
        self.methanol_v2 = self._define("met-v2", METHANOL_V2)
        self.sensory_v1 = self._define("sen-v1", SENSORY_V1)
        self.service.create_cycle(
            request_id="cycle-1", actor_id="admin", cycle_id="c1", name="三季度排名",
            period_start="2026-07-01", period_end="2026-09-30",
            metrics=[{"metric_id": "methanol", "metric_version_id": self.methanol_v2},
                     {"metric_id": "sensory", "metric_version_id": self.sensory_v1}])

    def tearDown(self):
        self.database.close()

    def _define(self, request_id, definition):
        receipt = self.service.define_metric_version(request_id=request_id, actor_id="admin",
                                                     **definition)
        self.service.activate_metric_version(request_id=f"{request_id}-act", actor_id="admin",
                                             metric_version_id=receipt.resource_id)
        return receipt.resource_id

    def _submit_all(self):
        self.service.submit_evidence(request_id="sub-a-met", actor_id="op-a", cycle_id="c1",
                                     site_id="site-a", metric_id="methanol",
                                     measurements=[
                                         {"batch_id": "BA-001", "product": "baijiu", "value": 10.0,
                                          "sampled_at": "2026-09-05"},
                                         {"batch_id": "BA-002", "product": "baijiu", "value": 12.0,
                                          "sampled_at": "2026-09-12"},
                                         {"batch_id": "BA-003", "product": "baijiu", "value": 40.0,
                                          "sampled_at": "2026-09-19"}])
        self.service.submit_evidence(request_id="sub-b-met", actor_id="op-b", cycle_id="c1",
                                     site_id="site-b", metric_id="methanol",
                                     measurements=[
                                         {"batch_id": "BB-001", "product": "baijiu", "value": 11.0,
                                          "sampled_at": "2026-09-06"},
                                         {"batch_id": "BB-002", "product": "baijiu", "value": 13.0,
                                          "sampled_at": "2026-09-13"}])
        self.service.submit_evidence(request_id="sub-a-sen", actor_id="op-a", cycle_id="c1",
                                     site_id="site-a", metric_id="sensory",
                                     measurements=[
                                         {"batch_id": "BA-001", "product": "baijiu", "value": 90.0,
                                          "sampled_at": "2026-09-05"},
                                         {"batch_id": "BA-002", "product": "baijiu", "value": 92.0,
                                          "sampled_at": "2026-09-12"}])
        self.service.submit_evidence(request_id="sub-b-sen", actor_id="op-b", cycle_id="c1",
                                     site_id="site-b", metric_id="sensory",
                                     measurements=[
                                         {"batch_id": "BB-001", "product": "baijiu", "value": 88.0,
                                          "sampled_at": "2026-09-06"}])

    def _freeze(self):
        self._submit_all()
        self.service.freeze_cycle(request_id="freeze-1", actor_id="admin", cycle_id="c1")

    def _exclude_ba003(self):
        exclusion = self.service.request_exclusion(
            request_id="exc-1", actor_id="op-a", cycle_id="c1", site_id="site-a",
            metric_id="methanol", batch_ids=["BA-003"], reason="校准期离群批次")
        self.service.review_exclusion(request_id="exc-1-review", actor_id="reviewer",
                                      exclusion_id=exclusion.resource_id, approve=True,
                                      note="校准记录属实")
        return exclusion.resource_id

    def _grant(self, request_id, target, permission):
        self.service.grant_permission(request_id=request_id, actor_id="admin",
                                      target_actor_id=target, permission=permission)

    # ---------- 指标版本化 ----------

    def test_method_change_keeps_version_history(self):
        versions = self.service.list_metric_versions("methanol")
        self.assertEqual(2, len(versions))
        self.assertEqual("retired", versions[0]["status"])
        self.assertEqual("active", versions[1]["status"])
        self.assertEqual("GC-2015", versions[0]["method_version"])
        self.assertEqual("GC-2026", versions[1]["method_version"])
        self.assertEqual(self.methanol_v1, versions[1]["supersedes_version_id"])

    def test_define_metric_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.service.define_metric_version(request_id="met-x", actor_id="op-a", **METHANOL_V1)

    def test_invalid_direction_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.define_metric_version(request_id="met-bad", actor_id="admin",
                                               **dict(METHANOL_V1, direction="sideways"))

    def test_unknown_adjustment_key_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.define_metric_version(request_id="met-bad2", actor_id="admin",
                                               **dict(METHANOL_V1, adjustment={"bonus": 1.0}))

    def test_cycle_cannot_pin_draft_version(self):
        draft = self.service.define_metric_version(request_id="met-draft", actor_id="admin",
                                                   **dict(METHANOL_V1, metric_id="acidity"))
        with self.assertRaises(ValidationError):
            self.service.create_cycle(request_id="cycle-bad", actor_id="admin", cycle_id="c2",
                                      name="四季度", period_start="2026-10-01",
                                      period_end="2026-12-31",
                                      metrics=[{"metric_id": "acidity",
                                                "metric_version_id": draft.resource_id}])

    # ---------- 证据提交与冻结 ----------

    def test_submission_validates_product_scope(self):
        with self.assertRaises(ValidationError):
            self.service.submit_evidence(request_id="sub-bad", actor_id="op-a", cycle_id="c1",
                                         site_id="site-a", metric_id="methanol",
                                         measurements=[{"batch_id": "BA-009", "product": "wine",
                                                        "value": 1.0, "sampled_at": "2026-09-05"}])

    def test_submission_validates_sampling_window(self):
        with self.assertRaises(ValidationError):
            self.service.submit_evidence(request_id="sub-bad2", actor_id="op-a", cycle_id="c1",
                                         site_id="site-a", metric_id="methanol",
                                         measurements=[{"batch_id": "BA-009", "product": "baijiu",
                                                        "value": 1.0, "sampled_at": "2026-10-05"}])

    def test_submission_requires_same_org(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_evidence(request_id="sub-bad3", actor_id="op-b", cycle_id="c1",
                                         site_id="site-a", metric_id="methanol",
                                         measurements=[{"batch_id": "BA-009", "product": "baijiu",
                                                        "value": 1.0, "sampled_at": "2026-09-05"}])

    def test_freeze_computes_scores_with_adjustments(self):
        self._freeze()
        scorecard = self.service.get_scorecard(actor_id="op-a", cycle_id="c1", site_id="site-a")
        methanol = next(item for item in scorecard["items"] if item["metric_id"] == "methanol")
        self.assertEqual(20.666667, methanol["raw_score"])
        self.assertEqual(20.166667, methanol["adjusted_score"])
        sensory = next(item for item in scorecard["items"] if item["metric_id"] == "sensory")
        self.assertEqual(91.0, sensory["adjusted_score"])

    def test_freeze_twice_rejected(self):
        self._freeze()
        with self.assertRaises(ConflictError):
            self.service.freeze_cycle(request_id="freeze-2", actor_id="admin", cycle_id="c1")

    def test_submission_after_freeze_rejected(self):
        self._freeze()
        with self.assertRaises(ConflictError):
            self.service.submit_evidence(request_id="sub-late", actor_id="op-a", cycle_id="c1",
                                         site_id="site-a", metric_id="methanol",
                                         measurements=[{"batch_id": "BA-009", "product": "baijiu",
                                                        "value": 1.0, "sampled_at": "2026-09-20"}])

    def test_same_request_replays_receipt(self):
        first = self.service.submit_evidence(
            request_id="sub-replay", actor_id="op-a", cycle_id="c1", site_id="site-a",
            metric_id="methanol",
            measurements=[{"batch_id": "BA-001", "product": "baijiu", "value": 10.0,
                           "sampled_at": "2026-09-05"}])
        second = self.service.submit_evidence(
            request_id="sub-replay", actor_id="op-a", cycle_id="c1", site_id="site-a",
            metric_id="methanol",
            measurements=[{"batch_id": "BA-001", "product": "baijiu", "value": 10.0,
                           "sampled_at": "2026-09-05"}])
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    # ---------- 异常值排除 ----------

    def test_exclusion_requires_independent_reviewer(self):
        self._freeze()
        exclusion = self.service.request_exclusion(
            request_id="exc-1", actor_id="op-a", cycle_id="c1", site_id="site-a",
            metric_id="methanol", batch_ids=["BA-003"], reason="离群批次")
        with self.assertRaises(PermissionDenied):
            self.service.review_exclusion(request_id="exc-bad", actor_id="reviewer-fa",
                                          exclusion_id=exclusion.resource_id, approve=True)
        with self.assertRaises(PermissionDenied):
            self.service.review_exclusion(request_id="exc-self", actor_id="op-a",
                                          exclusion_id=exclusion.resource_id, approve=True)

    def test_exclusion_approval_recomputes_and_records_impact(self):
        self._freeze()
        exclusion_id = self._exclude_ba003()
        exclusions = self.service.list_exclusions(actor_id="admin", cycle_id="c1")
        self.assertEqual("approved", exclusions[0]["status"])
        impact = exclusions[0]["impact"]
        self.assertEqual(2, impact["metric_ranks_before"]["site-a"]["rank"])
        self.assertEqual(1, impact["metric_ranks_before"]["site-b"]["rank"])
        self.assertEqual(1, impact["metric_ranks_after"]["site-a"]["rank"])
        self.assertEqual(2, impact["metric_ranks_after"]["site-b"]["rank"])
        self.assertIn("totals_before", impact)
        self.assertIn("totals_after", impact)
        scorecard = self.service.get_scorecard(actor_id="op-a", cycle_id="c1", site_id="site-a")
        methanol = next(item for item in scorecard["items"] if item["metric_id"] == "methanol")
        self.assertEqual(11.0, methanol["raw_score"])
        self.assertEqual(10.5, methanol["adjusted_score"])
        self.assertEqual(["BA-003"], methanol["excluded_batches"])
        self.assertEqual(exclusion_id, exclusions[0]["exclusion_id"])

    def test_exclusion_rejection_keeps_scores(self):
        self._freeze()
        exclusion = self.service.request_exclusion(
            request_id="exc-1", actor_id="op-a", cycle_id="c1", site_id="site-a",
            metric_id="methanol", batch_ids=["BA-003"], reason="离群批次")
        self.service.review_exclusion(request_id="exc-1-review", actor_id="reviewer",
                                      exclusion_id=exclusion.resource_id, approve=False,
                                      note="校准记录不足")
        scorecard = self.service.get_scorecard(actor_id="op-a", cycle_id="c1", site_id="site-a")
        methanol = next(item for item in scorecard["items"] if item["metric_id"] == "methanol")
        self.assertEqual(20.166667, methanol["adjusted_score"])
        self.assertEqual([], methanol["excluded_batches"])

    def test_pending_exclusion_blocks_duplicate(self):
        self._freeze()
        self.service.request_exclusion(request_id="exc-1", actor_id="op-a", cycle_id="c1",
                                       site_id="site-a", metric_id="methanol",
                                       batch_ids=["BA-003"], reason="离群批次")
        with self.assertRaises(ConflictError):
            self.service.request_exclusion(request_id="exc-2", actor_id="op-a", cycle_id="c1",
                                           site_id="site-a", metric_id="methanol",
                                           batch_ids=["BA-002"], reason="另一申请")

    def test_excluding_all_batches_marks_snapshot_excluded(self):
        self._freeze()
        exclusion = self.service.request_exclusion(
            request_id="exc-all", actor_id="op-b", cycle_id="c1", site_id="site-b",
            metric_id="sensory", batch_ids=["BB-001"], reason="样本污染")
        self.service.review_exclusion(request_id="exc-all-review", actor_id="reviewer",
                                      exclusion_id=exclusion.resource_id, approve=True)
        scorecard = self.service.get_scorecard(actor_id="op-b", cycle_id="c1", site_id="site-b")
        sensory = next(item for item in scorecard["items"] if item["metric_id"] == "sensory")
        self.assertTrue(sensory["excluded"])

    # ---------- 异议与发布 ----------

    def test_dispute_suspends_only_disputed_metric_on_publish(self):
        self._freeze()
        self._exclude_ba003()
        self.service.raise_dispute(request_id="dis-1", actor_id="op-b", cycle_id="c1",
                                   site_id="site-b", metric_id="sensory", reason="封存链不完整")
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        self._grant("grant-view", "exec", "ranking.view")
        ranking = self.service.get_ranking(actor_id="exec", cycle_id="c1")
        statuses = {item["metric_id"]: item["publication"]["status"] for item in ranking["metrics"]}
        self.assertEqual({"methanol": "published", "sensory": "suspended"}, statuses)
        methanol = next(item for item in ranking["metrics"] if item["metric_id"] == "methanol")
        self.assertEqual(1, methanol["entries"][0]["rank"])
        self.assertEqual("site-a", methanol["entries"][0]["site_id"])
        totals = {entry["site_id"]: entry for entry in ranking["totals"]["entries"]}
        self.assertEqual(100.0, totals["site-a"]["total_score"])
        self.assertEqual(0.0, totals["site-b"]["total_score"])

    def test_dispute_after_publish_auto_suspends_only_that_metric(self):
        self._freeze()
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        self.service.raise_dispute(request_id="dis-1", actor_id="op-b", cycle_id="c1",
                                   site_id="site-b", metric_id="sensory", reason="结果有误")
        self._grant("grant-view", "exec", "ranking.view")
        ranking = self.service.get_ranking(actor_id="exec", cycle_id="c1")
        statuses = {item["metric_id"]: item["publication"]["status"] for item in ranking["metrics"]}
        self.assertEqual({"methanol": "published", "sensory": "suspended"}, statuses)

    def test_resolve_dispute_rejected_allows_republish(self):
        self._freeze()
        dispute = self.service.raise_dispute(request_id="dis-1", actor_id="op-b", cycle_id="c1",
                                             site_id="site-b", metric_id="sensory", reason="结果有误")
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        self.service.resolve_dispute(request_id="dis-1-resolve", actor_id="reviewer",
                                     dispute_id=dispute.resource_id, outcome="rejected",
                                     note="复核后异议不成立")
        self.service.publish_cycle(request_id="pub-2", actor_id="admin", cycle_id="c1")
        self._grant("grant-view", "exec", "ranking.view")
        ranking = self.service.get_ranking(actor_id="exec", cycle_id="c1")
        statuses = {item["metric_id"]: item["publication"]["status"] for item in ranking["metrics"]}
        self.assertEqual({"methanol": "published", "sensory": "published"}, statuses)
        sensory = next(item for item in ranking["metrics"] if item["metric_id"] == "sensory")
        self.assertEqual(2, sensory["publication"]["version"])

    def test_resolve_dispute_upheld_suspends_snapshot(self):
        self._freeze()
        dispute = self.service.raise_dispute(request_id="dis-1", actor_id="op-b", cycle_id="c1",
                                             site_id="site-b", metric_id="sensory", reason="样本污染")
        self.service.resolve_dispute(request_id="dis-1-resolve", actor_id="reviewer",
                                     dispute_id=dispute.resource_id, outcome="upheld",
                                     note="样本确被污染")
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        self._grant("grant-view", "exec", "ranking.view")
        ranking = self.service.get_ranking(actor_id="exec", cycle_id="c1")
        sensory = next(item for item in ranking["metrics"] if item["metric_id"] == "sensory")
        entries = {entry["site_id"]: entry for entry in sensory["entries"]}
        self.assertFalse(entries["site-b"]["included"])
        self.assertIsNone(entries["site-b"]["rank"])
        self.assertTrue(entries["site-a"]["included"])

    def test_dispute_requires_independent_resolver(self):
        self._freeze()
        dispute = self.service.raise_dispute(request_id="dis-1", actor_id="op-b", cycle_id="c1",
                                             site_id="site-b", metric_id="sensory", reason="结果有误")
        with self.assertRaises(PermissionDenied):
            self.service.resolve_dispute(request_id="dis-bad", actor_id="op-b",
                                         dispute_id=dispute.resource_id, outcome="rejected",
                                         note="自行处理")

    def test_publish_requires_frozen_cycle(self):
        with self.assertRaises(ConflictError):
            self.service.publish_cycle(request_id="pub-early", actor_id="admin", cycle_id="c1")

    # ---------- 整改闭环 ----------

    def test_corrective_plan_lifecycle(self):
        self._freeze()
        exclusion_id = self._exclude_ba003()
        plan = self.service.create_corrective_plan(
            request_id="plan-1", actor_id="op-a", finding_type="exclusion",
            finding_id=exclusion_id, site_id="site-a", owner_actor_id="op-a",
            description="校准期批次隔离整改", deadline="2026-10-15")
        with self.assertRaises(ConflictError):
            self.service.verify_corrective_plan(request_id="plan-early", actor_id="reviewer",
                                                plan_id=plan.resource_id, approve=True)
        self.service.submit_plan_evidence(request_id="plan-1-ev", actor_id="op-a",
                                          plan_id=plan.resource_id, description="复检报告",
                                          evidence={"report_id": "REC-001", "result": "passed"})
        self.service.verify_corrective_plan(request_id="plan-1-verify", actor_id="reviewer",
                                            plan_id=plan.resource_id, approve=True, note="复验合格")
        plans = self.service.list_plans(actor_id="admin", site_id="site-a")
        self.assertEqual("verified", plans[0]["status"])
        self.assertEqual("op-a", plans[0]["owner_actor_id"])
        self.assertEqual("2026-10-15", plans[0]["deadline"])
        self.assertEqual("exclusion", plans[0]["finding_type"])
        self.assertEqual(1, len(plans[0]["evidence"]))
        with self.assertRaises(ConflictError):
            self.service.submit_plan_evidence(request_id="plan-1-ev2", actor_id="op-a",
                                              plan_id=plan.resource_id, description="补充",
                                              evidence={"note": "late"})

    def test_plan_verification_does_not_rewrite_published_ranking(self):
        self._freeze()
        exclusion_id = self._exclude_ba003()
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        self._grant("grant-view", "exec", "ranking.view")
        before = self.service.get_ranking(actor_id="exec", cycle_id="c1")
        plan = self.service.create_corrective_plan(
            request_id="plan-1", actor_id="op-a", finding_type="exclusion",
            finding_id=exclusion_id, site_id="site-a", owner_actor_id="op-a",
            description="整改", deadline="2026-10-15")
        self.service.submit_plan_evidence(request_id="plan-1-ev", actor_id="op-a",
                                          plan_id=plan.resource_id, description="复检报告",
                                          evidence={"report_id": "REC-001"})
        self.service.verify_corrective_plan(request_id="plan-1-verify", actor_id="reviewer",
                                            plan_id=plan.resource_id, approve=True)
        after = self.service.get_ranking(actor_id="exec", cycle_id="c1")
        self.assertEqual(before, after)

    def test_plan_owner_must_belong_to_factory_org(self):
        self._freeze()
        exclusion_id = self._exclude_ba003()
        with self.assertRaises(ValidationError):
            self.service.create_corrective_plan(
                request_id="plan-bad", actor_id="op-a", finding_type="exclusion",
                finding_id=exclusion_id, site_id="site-a", owner_actor_id="exec",
                description="整改", deadline="2026-10-15")

    def test_plan_verify_requires_independence(self):
        self._freeze()
        exclusion_id = self._exclude_ba003()
        plan = self.service.create_corrective_plan(
            request_id="plan-1", actor_id="op-a", finding_type="exclusion",
            finding_id=exclusion_id, site_id="site-a", owner_actor_id="op-a",
            description="整改", deadline="2026-10-15")
        self.service.submit_plan_evidence(request_id="plan-1-ev", actor_id="op-a",
                                          plan_id=plan.resource_id, description="复检报告",
                                          evidence={"report_id": "REC-001"})
        with self.assertRaises(PermissionDenied):
            self.service.verify_corrective_plan(request_id="plan-bad", actor_id="reviewer-fa",
                                                plan_id=plan.resource_id, approve=True)

    def test_plan_finding_must_match_site(self):
        self._freeze()
        exclusion_id = self._exclude_ba003()
        with self.assertRaises(ValidationError):
            self.service.create_corrective_plan(
                request_id="plan-bad2", actor_id="op-b", finding_type="exclusion",
                finding_id=exclusion_id, site_id="site-b", owner_actor_id="op-b",
                description="整改", deadline="2026-10-15")

    # ---------- 追溯与权限分离 ----------

    def test_trace_reaches_batches_and_rules(self):
        self._freeze()
        self._exclude_ba003()
        self._grant("grant-trace", "exec", "ranking.trace")
        trace = self.service.trace_score(actor_id="exec", cycle_id="c1", site_id="site-a",
                                         metric_id="methanol", purpose="ranking")
        self.assertEqual(["BA-001", "BA-002"], trace["score"]["computation"]["batches"])
        self.assertEqual(["BA-003"], trace["score"]["computation"]["excluded_batches"])
        self.assertEqual("GC-2026", trace["metric_version"]["method_version"])
        self.assertEqual(-0.5, trace["metric_version"]["adjustment"]["method_offset"])
        self.assertEqual(3, len(trace["evidence"]["measurements"]))
        self.assertEqual(1, len(trace["exclusions"]))

    def test_trace_rejects_batch_release_purpose(self):
        self._freeze()
        self._grant("grant-trace", "exec", "ranking.trace")
        with self.assertRaises(PermissionDenied):
            self.service.trace_score(actor_id="exec", cycle_id="c1", site_id="site-a",
                                     metric_id="methanol", purpose="batch_release")

    def test_trace_requires_permission(self):
        self._freeze()
        with self.assertRaises(PermissionDenied):
            self.service.trace_score(actor_id="exec", cycle_id="c1", site_id="site-a",
                                     metric_id="methanol", purpose="ranking")

    def test_ranking_view_requires_permission(self):
        self._freeze()
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        with self.assertRaises(PermissionDenied):
            self.service.get_ranking(actor_id="exec", cycle_id="c1")
        self._grant("grant-view", "exec", "ranking.view")
        ranking = self.service.get_ranking(actor_id="exec", cycle_id="c1")
        self.assertEqual("published", ranking["cycle_status"])

    def test_release_decision_requires_release_permission(self):
        self._freeze()
        self._grant("grant-view", "exec", "ranking.view")
        self._grant("grant-trace", "exec", "ranking.trace")
        with self.assertRaises(PermissionDenied):
            self.service.record_release_decision(request_id="rel-1", actor_id="exec",
                                                 site_id="site-a", batch_id="BA-001",
                                                 decision="released", basis={"doc": "QA-1"})
        self._grant("grant-release", "releaser", "batch.release")
        receipt = self.service.record_release_decision(request_id="rel-2", actor_id="releaser",
                                                       site_id="site-a", batch_id="BA-001",
                                                       decision="released", basis={"doc": "QA-1"})
        self.assertFalse(receipt.replayed)
        with self.assertRaises(ConflictError):
            self.service.record_release_decision(request_id="rel-3", actor_id="releaser",
                                                 site_id="site-a", batch_id="BA-001",
                                                 decision="rejected", basis={"doc": "QA-2"})

    def test_release_role_cannot_view_rankings(self):
        self._freeze()
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        self._grant("grant-release", "releaser", "batch.release")
        with self.assertRaises(PermissionDenied):
            self.service.get_ranking(actor_id="releaser", cycle_id="c1")

    def test_scorecard_limited_to_own_org(self):
        self._freeze()
        with self.assertRaises(PermissionDenied):
            self.service.get_scorecard(actor_id="op-b", cycle_id="c1", site_id="site-a")
        scorecard = self.service.get_scorecard(actor_id="op-a", cycle_id="c1", site_id="site-a")
        self.assertEqual(2, len(scorecard["items"]))

    def test_trace_unknown_score_returns_not_found(self):
        self._freeze()
        self._grant("grant-trace", "exec", "ranking.trace")
        with self.assertRaises(NotFoundError):
            self.service.trace_score(actor_id="exec", cycle_id="c1", site_id="site-a",
                                     metric_id="unknown", purpose="ranking")

    # ---------- 审计 ----------

    def test_audit_chain_valid_after_full_flow(self):
        self._freeze()
        self._exclude_ba003()
        dispute = self.service.raise_dispute(request_id="dis-1", actor_id="op-b", cycle_id="c1",
                                             site_id="site-b", metric_id="sensory", reason="异议")
        self.service.publish_cycle(request_id="pub-1", actor_id="admin", cycle_id="c1")
        self.service.resolve_dispute(request_id="dis-1-resolve", actor_id="reviewer",
                                     dispute_id=dispute.resource_id, outcome="rejected",
                                     note="不成立")
        self.service.publish_cycle(request_id="pub-2", actor_id="admin", cycle_id="c1")
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
