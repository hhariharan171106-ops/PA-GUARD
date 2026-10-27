import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import database
from fastapi import HTTPException
from case_workflow import calculate_sla_risk_assessment
from sla_deterministic_risk import assess_case_operational_risk
from escalation_agents import evaluate_rules


class DeterministicSlaRiskTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path_patcher = patch.object(database, "DB_PATH", Path(self.temp_dir.name) / "risk.db")
        self.db_path_patcher.start()
        self.addCleanup(self.db_path_patcher.stop)
        self.connect_patcher = patch.object(database, "connect", self._closing_connect)
        self.connect_patcher.start()
        self.addCleanup(self.connect_patcher.stop)
        database.initialize()
        with database.connect() as db:
            db.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("CASE-RISK", "SYNTHETIC", "Payer", "Service", "2026-10-01",
                 (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
                 "EVIDENCE REVIEW", "ON TRACK", "Coordinator", "MEDIUM"),
            )

    @staticmethod
    def _closing_connect():
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc_value, traceback):
                try:
                    return super().__exit__(exc_type, exc_value, traceback)
                finally:
                    self.close()

        connection = sqlite3.connect(database.DB_PATH, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def test_escalation_threshold_and_reason_are_reported(self):
        now = datetime.now(timezone.utc)
        result = assess_case_operational_risk("CASE-RISK", connection_factory=self._closing_connect, now=now)
        self.assertEqual(result["risk_category"], "HIGH")
        self.assertTrue(result["escalation_required"])
        self.assertEqual(result["configuration"]["rule_version"], "sla-operational-risk-v1")
        self.assertTrue(any("escalation tier 1" in reason for reason in result["reasons"]))

    def test_overdue_case_is_deterministically_breached(self):
        now = datetime.now(timezone.utc)
        with database.connect() as db:
            db.execute("UPDATE cases SET deadline=? WHERE id=?", ((now - timedelta(minutes=1)).isoformat(), "CASE-RISK"))
        result = assess_case_operational_risk("CASE-RISK", connection_factory=self._closing_connect, now=now)
        self.assertEqual(result["risk_category"], "BREACHED")
        self.assertTrue(result["breached"])
        self.assertTrue(result["escalation_required"])

    def test_resolved_case_is_not_reported_as_active_breach(self):
        now = datetime.now(timezone.utc)
        with database.connect() as db:
            db.execute("UPDATE cases SET deadline=?,status='APPROVED' WHERE id=?", ((now - timedelta(hours=2)).isoformat(), "CASE-RISK"))
        result = assess_case_operational_risk("CASE-RISK", connection_factory=self._closing_connect, now=now)
        self.assertEqual(result["risk_category"], "RESOLVED")
        self.assertFalse(result["breached"])
        self.assertFalse(result["escalation_required"])

    def test_identical_inputs_return_identical_result(self):
        now = datetime.now(timezone.utc)
        first = assess_case_operational_risk("CASE-RISK", connection_factory=self._closing_connect, now=now)
        second = assess_case_operational_risk("CASE-RISK", connection_factory=self._closing_connect, now=now)
        self.assertEqual(first, second)

    def test_workflow_keeps_deterministic_risk_when_ml_is_unavailable(self):
        operational = {"risk_category": "HIGH", "breached": False}

        def unavailable(_case_id, **_kwargs):
            raise HTTPException(503, "SLA model artifact is missing")

        result = calculate_sla_risk_assessment(
            "CASE-RISK",
            prediction_function=unavailable,
            operational_risk_function=lambda _case_id, **_kwargs: operational,
        )
        self.assertFalse(result["available"])
        self.assertIsNone(result["breach_probability"])
        self.assertIs(result["operational_risk"], operational)

    def test_high_synthetic_ml_probability_does_not_overwrite_medium_operational_risk(self):
        operational = {"risk_category": "MEDIUM", "breached": False, "escalation_required": False}
        prediction = {
            "case_id": "CASE-RISK", "available": True, "risk_level": "HIGH",
            "breach_probability": 65.6, "risk_score": 65.6,
            "reliable": False, "training_data_source": "synthetic scenarios",
        }
        result = calculate_sla_risk_assessment(
            "CASE-RISK",
            prediction_function=lambda *_args, **_kwargs: dict(prediction),
            operational_risk_function=lambda *_args, **_kwargs: operational,
        )
        self.assertEqual(result["risk_level"], "HIGH")
        self.assertEqual(result["breach_probability"], 65.6)
        self.assertEqual(result["operational_risk"]["risk_category"], "MEDIUM")
        self.assertFalse(result["operational_risk"]["escalation_required"])

    def test_elevated_ml_risk_does_not_trigger_formal_escalation_before_threshold(self):
        now = datetime.now(timezone.utc)
        result = evaluate_rules({
            "case": {"id": "CASE-RISK", "status": "EVIDENCE REVIEW", "risk": "AT RISK",
                     "deadline": (now + timedelta(hours=20)).isoformat(), "coordinator": "Local ML Test"},
            "now": now.isoformat(), "settings": {"escalation_one_hours": 4, "escalation_two_hours": 0},
            "attempts": [], "policies": [], "policy_valid": True,
            "level_roles": {"LEVEL_1": "Supervisor", "LEVEL_2": "Manager"},
        })["result"]
        self.assertFalse(result["escalation_required"])
        self.assertEqual(result["escalation_level"], "NONE")
        self.assertEqual(result["responsible_role"], "")
        self.assertIn("formal escalation threshold", result["recommended_action"])

    def test_deadline_threshold_assigns_configured_escalation_role(self):
        now = datetime.now(timezone.utc)
        result = evaluate_rules({
            "case": {"id": "CASE-RISK", "status": "EVIDENCE REVIEW", "risk": "AT RISK",
                     "deadline": (now + timedelta(hours=3)).isoformat(), "coordinator": "Local ML Test"},
            "now": now.isoformat(), "settings": {"escalation_one_hours": 4, "escalation_two_hours": 0},
            "attempts": [], "policies": [], "policy_valid": True,
            "level_roles": {"LEVEL_1": "Configured Supervisor", "LEVEL_2": "Configured Manager"},
        })["result"]
        self.assertTrue(result["escalation_required"])
        self.assertEqual(result["responsible_role"], "Configured Supervisor")
        self.assertNotIn("Local ML Test", result["responsible_role"])

    def test_outer_configured_tier_counts_as_formal_escalation_window(self):
        now = datetime.now(timezone.utc)
        with database.connect() as db:
            db.execute("UPDATE sla_settings SET escalation_one_hours=2,escalation_two_hours=10 WHERE id=1")
            db.execute("UPDATE cases SET deadline=? WHERE id=?", ((now + timedelta(hours=8)).isoformat(), "CASE-RISK"))
        result = assess_case_operational_risk("CASE-RISK", connection_factory=self._closing_connect, now=now)
        self.assertTrue(result["escalation_required"])
        self.assertEqual(result["risk_category"], "CRITICAL")


if __name__ == "__main__":
    unittest.main()
