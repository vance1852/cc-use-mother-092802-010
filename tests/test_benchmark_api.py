"""跨工厂质量对标平台的 HTTP 路由测试。"""

import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.benchmark import BenchmarkService
from beverage_ops_foundation.storage import Database


class BenchmarkApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = BenchmarkService(self.database)
        r = route(self.service, "POST", "/organizations",
                  {"request_id": "org", "organization_id": "o1", "name": "集团"},
                  {"X-Actor-Id": "bootstrap"})
        self.assertEqual(201, r[0])
        for rid, aid, name, role in [
            ("admin", "ad1", "管理员", "admin"),
            ("qm", "qm1", "质量总监", "quality_manager"),
            ("op1", "op1", "一厂操作员", "operator"),
            ("rel", "rel1", "放行人", "release_officer"),
        ]:
            status, _ = route(self.service, "POST", "/actors",
                              {"request_id": rid, "new_actor_id": aid, "display_name": name,
                               "role": role, "organization_id": "o1"},
                              {"X-Actor-Id": "bootstrap" if aid == "ad1" else "ad1"})
            self.assertIn(status, (200, 201))
        route(self.service, "POST", "/sites",
              {"request_id": "s1", "site_id": "site-1", "organization_id": "o1",
               "name": "一厂", "timezone_name": "Asia/Shanghai"},
              {"X-Actor-Id": "op1"})

    def tearDown(self):
        self.database.close()

    def _metric_and_cycle(self):
        status, body = route(self.service, "POST", "/metrics", {
            "request_id": "m1", "metric_id": "purity", "code": "PURITY", "name": "纯度合格率",
            "direction": "higher_better", "weight": 1, "applicable_products": ["liquor-a"],
            "sampling": {"basis": "batch"}, "method_code": "GB-P", "method_version": "2021",
            "rule": {"type": "pass_rate"}}, {"X-Actor-Id": "qm1"})
        self.assertEqual(201, status)
        status, body = route(self.service, "POST", "/cycles", {
            "request_id": "c1", "cycle_id": "cyc-1", "code": "2026Q3",
            "period_start": "2026-07-01", "period_end": "2026-09-30"}, {"X-Actor-Id": "qm1"})
        self.assertEqual(201, status)
        route(self.service, "POST", "/cycles/metrics",
              {"request_id": "cm1", "cycle_id": "cyc-1", "metric_id": "purity"},
              {"X-Actor-Id": "qm1"})
        route(self.service, "POST", "/cycles/sites",
              {"request_id": "cs1", "cycle_id": "cyc-1", "site_id": "site-1"},
              {"X-Actor-Id": "qm1"})

    def test_metric_versioning_endpoint(self):
        self._metric_and_cycle()
        status, body = route(self.service, "GET", "/metrics?metric_id=purity", None, {})
        self.assertEqual(200, status)
        self.assertEqual(1, body["version"])
        self.assertEqual("2021", body["method_version"])

    def test_full_publish_flow_over_http(self):
        self._metric_and_cycle()
        for index, passed in enumerate([True, True, False]):
            status, _ = route(self.service, "POST", "/evidence", {
                "request_id": f"ev{index}", "cycle_id": "cyc-1", "site_id": "site-1",
                "metric_id": "purity", "batch_no": f"B{index}", "product_code": "liquor-a",
                "sampled_at": "2026-08-01T00:00:00Z", "raw_value": 95 if passed else 50,
                "payload": {"pass": passed}}, {"X-Actor-Id": "op1"})
            self.assertEqual(201, status)
        status, body = route(self.service, "POST", "/cycles/freeze",
                             {"request_id": "fz1", "cycle_id": "cyc-1"}, {"X-Actor-Id": "qm1"})
        self.assertEqual(200, status)
        self.assertEqual("frozen", body["status"])
        status, body = route(self.service, "POST", "/cycles/publish",
                             {"request_id": "pub1", "cycle_id": "cyc-1"}, {"X-Actor-Id": "qm1"})
        self.assertEqual(201, status)
        status, body = route(self.service, "GET", "/publications?cycle_id=cyc-1", None, {})
        self.assertEqual(200, status)
        self.assertAlmostEqual(66.6667, body["rows"][0]["score"])

    def test_trace_score_endpoint_links_batches(self):
        self._metric_and_cycle()
        route(self.service, "POST", "/evidence", {
            "request_id": "ev1", "cycle_id": "cyc-1", "site_id": "site-1",
            "metric_id": "purity", "batch_no": "B1", "product_code": "liquor-a",
            "sampled_at": "2026-08-01T00:00:00Z", "raw_value": 95,
            "payload": {"pass": True}}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/cycles/freeze",
              {"request_id": "fz1", "cycle_id": "cyc-1"}, {"X-Actor-Id": "qm1"})
        status, body = route(self.service, "GET",
                             "/trace-score?cycle_id=cyc-1&site_id=site-1&metric_id=purity", None, {})
        self.assertEqual(200, status)
        self.assertEqual("B1", body["evidence"][0]["batch_no"])
        self.assertEqual("2021", body["metric_definition"]["method_version"])
        self.assertEqual("pass_rate", body["frozen"]["calc"]["rule"]["type"])

    def test_ranking_role_cannot_release_over_http(self):
        status, body = route(self.service, "POST", "/batch-releases", {
            "request_id": "rb1", "site_id": "site-1", "batch_no": "B1",
            "product_code": "liquor-a", "decision": "released", "reason": "合格"},
            {"X-Actor-Id": "qm1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])
        status, body = route(self.service, "POST", "/batch-releases", {
            "request_id": "rb2", "site_id": "site-1", "batch_no": "B1",
            "product_code": "liquor-a", "decision": "released", "reason": "合格"},
            {"X-Actor-Id": "rel1"})
        self.assertEqual(201, status)

    def test_unknown_benchmark_route_falls_through_to_404(self):
        status, body = route(self.service, "GET", "/nope", None, {})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", body["error"])


if __name__ == "__main__":
    unittest.main()
