import unittest

from quality_benchmark.api import route
from quality_benchmark.service import QualityService
from quality_benchmark.storage import QualityDatabase


class QualityApiTest(unittest.TestCase):
    def setUp(self):
        self.database = QualityDatabase()
        self.service = QualityService(self.database)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="hq", name="集团质量中心")
        self.service.register_actor(request_id="actor", actor_id="bootstrap", new_actor_id="admin",
                                    display_name="管理员", role="admin", organization_id="hq")

    def tearDown(self):
        self.database.close()

    def test_foundation_health_still_available(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_quality_route_returns_404(self):
        status, payload = route(self.service, "GET", "/quality/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_define_metric_version_via_http(self):
        body = {"request_id": "met-1", "metric_id": "methanol", "name": "甲醇含量",
                "unit": "mg/L", "direction": "lower_better", "weight": 0.6,
                "applicable_products": ["baijiu"],
                "sampling_window": {"start": "2026-08-01", "end": "2026-09-30"},
                "method_version": "GC-2026", "adjustment": {"method_offset": -0.5}}
        status, payload = route(self.service, "POST", "/quality/metric-versions", body,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(201, status)
        self.assertEqual("metric_version", payload["resource_type"])
        replay, _ = route(self.service, "POST", "/quality/metric-versions", body,
                          {"X-Actor-Id": "admin"})
        self.assertEqual(200, replay)

    def test_rankings_require_actor(self):
        status, payload = route(self.service, "GET", "/quality/rankings?cycle_id=c1", None)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_trace_rejects_release_purpose(self):
        status, payload = route(
            self.service, "GET",
            "/quality/trace?cycle_id=c1&site_id=site-a&metric_id=methanol&purpose=batch_release",
            None, {"X-Actor-Id": "admin"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_invalid_body_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/quality/cycles", {"request_id": "x"},
                                {"X-Actor-Id": "admin"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


if __name__ == "__main__":
    unittest.main()
