import unittest

from quality_benchmark.acceptance import run


class QualityAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(all(result["checks"].values()))


if __name__ == "__main__":
    unittest.main()
