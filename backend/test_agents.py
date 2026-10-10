import unittest
from datetime import datetime, timezone

from database import initialize
from agents import run_sla_rag


class SlaRagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        initialize()

    def test_breached_case_is_deterministic(self) -> None:
        result = run_sla_rag("PA-1041", datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["risk_level"], "BREACHED")
        self.assertEqual(result["remaining_hours"], -1.0)
        self.assertTrue(result["needs_human_review"])
        self.assertGreater(len(result["source_references"]), 0)

    def test_agent_never_returns_authorization_decision(self) -> None:
        result = run_sla_rag("PA-1042", datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc))
        self.assertNotIn(result["recommended_action"].lower(), {"approve", "deny"})
        self.assertIn(result["risk_level"], {"LOW", "MEDIUM", "HIGH", "BREACHED", "NEEDS_REVIEW"})


if __name__ == "__main__":
    unittest.main()
