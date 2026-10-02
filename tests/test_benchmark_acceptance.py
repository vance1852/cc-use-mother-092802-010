import unittest

from beverage_ops_foundation.benchmark_acceptance import run


class BenchmarkAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"], msg=result["checks"])
        self.assertTrue(result["checks"]["audit_valid"])
        self.assertTrue(result["checks"]["v1_held_only_disputed_cell"])
        self.assertTrue(result["checks"]["release_permission_separated"])
        self.assertTrue(result["checks"]["trace_has_batches_rule_hash"])


if __name__ == "__main__":
    unittest.main()
