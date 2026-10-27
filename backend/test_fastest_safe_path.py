from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import approval_api
import database
import main
import security
from services.fastest_safe_path import build_plan


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
CONFIG = json.loads((Path(__file__).parent / "config" / "fastest_safe_path.json").read_text())


def case(deadline=NOW + timedelta(hours=2), status="NEW"):
    return {"id": "PA-PLAN-1", "deadline": deadline.isoformat() if deadline else None, "status": status}


class PlannerUnitTests(unittest.TestCase):
    def plan(self, states=(), policy="VERIFIED", role="SUPERVISOR", deadline=NOW + timedelta(hours=2), conf=None, docs=()):
        return build_plan(case(deadline), list(states), list(docs), policy, role, NOW, CONFIG if conf is None else conf)

    def test_possible_match_achievable_and_deterministic(self):
        states = [{"criterion_id": "C-1", "status": "MATCH_POSSIBLE", "document_id": 2}]
        a = self.plan(states=states)
        b = self.plan(states=states)
        self.assertEqual(a.model_dump(), b.model_dump())
        self.assertEqual(a.verdict, "ACHIEVABLE")
        self.assertEqual(a.steps[0].action_type, "CONFIRM_EVIDENCE_MATCH")

    def test_unknown_external_dependency_and_infeasible_extension(self):
        enabled = json.loads(json.dumps(CONFIG))
        enabled["actions"]["REQUEST_MISSING_DOCUMENT"]["workflow_supported"] = True
        result = self.plan(states=[{"criterion_id": "C-1", "status": "NO_MATCH", "document_id": 9}], docs=[{"id": 9, "processing_status": "COMPLETED", "ocr_status": "COMPLETED"}], deadline=NOW + timedelta(minutes=3), conf=enabled)
        self.assertEqual(result.verdict, "UNCERTAIN")
        self.assertTrue(result.external_wait_unknown)
        self.assertIsNone(result.critical_path_minutes)
        config = json.loads(json.dumps(enabled))
        config["actions"]["REQUEST_MISSING_DOCUMENT"]["estimated_minutes"] = 30
        config["actions"]["REQUEST_MISSING_DOCUMENT"]["duration_known"] = True
        config["actions"]["REQUEST_MISSING_DOCUMENT"]["external_dependency"] = False
        result = self.plan(states=[{"criterion_id": "C-1", "status": "NO_MATCH", "document_id": 9}], docs=[{"id": 9}], deadline=NOW + timedelta(minutes=5), conf=config)
        self.assertEqual(result.verdict, "INFEASIBLE_WITHOUT_EXTENSION")
        self.assertIn("ESCALATE_FOR_PAYER_EXTENSION", [step.action_type for step in result.steps])
        self.assertIn("does not grant", result.steps[-1].description)

    def test_no_missing_document_action_without_existing_workflow(self):
        result = self.plan(states=[{"criterion_id": "C-1", "status": "NO_MATCH", "document_id": 9}], docs=[{"id": 9, "processing_status": "COMPLETED", "ocr_status": "COMPLETED"}])
        self.assertEqual(result.verdict, "UNCERTAIN")
        self.assertFalse(result.external_wait_unknown)
        self.assertTrue(any("No supported workflow action" in gap for gap in result.unresolved_gaps))

    def test_tight_and_permission_blocked(self):
        config = json.loads(json.dumps(CONFIG))
        config["actions"]["CONFIRM_EVIDENCE_MATCH"]["estimated_minutes"] = 50
        tight = self.plan(states=[{"criterion_id": "C-1", "status": "MATCH_POSSIBLE"}], deadline=NOW + timedelta(minutes=60), conf=config)
        self.assertEqual(tight.verdict, "TIGHT")
        blocked = self.plan(states=[{"criterion_id": "C-1", "status": "MATCH_POSSIBLE"}], role="SPECIALIST")
        self.assertEqual(blocked.verdict, "BLOCKED_BY_PERMISSION")
        self.assertFalse(blocked.steps[0].can_current_user_perform)

    def test_no_gaps_unsupported_unverified_unknown_and_rejected(self):
        clear = self.plan(states=[{"criterion_id": "C", "status": "HUMAN_CONFIRMED"}])
        self.assertEqual(clear.verdict, "READY_FOR_HUMAN_SUBMISSION_REVIEW")
        for state in ("UNSUPPORTED_CRITERION", "MYSTERY"):
            result = self.plan(states=[{"criterion_id": "C", "status": state}])
            self.assertEqual(result.verdict, "UNCERTAIN")
            self.assertEqual(result.steps, [])
        config = json.loads(json.dumps(CONFIG))
        config["actions"]["RESOLVE_POLICY_VERIFICATION"]["estimated_minutes"] = 30
        unverified = self.plan(policy="UNVERIFIED", role="MANAGER", deadline=NOW + timedelta(minutes=5), conf=config)
        self.assertEqual(unverified.verdict, "INFEASIBLE_WITHOUT_EXTENSION")
        self.assertTrue(any("unverified" in gap for gap in unverified.unresolved_gaps))
        rejected = self.plan(states=[{"criterion_id": "C", "status": "UNCERTAIN"}])
        self.assertEqual(rejected.verdict, "ACHIEVABLE")
        self.assertNotEqual(rejected.steps[0].action_type, "CONFIRM_EVIDENCE_MATCH")

    def test_terminal_missing_policy_and_pending_manager_review(self):
        terminal = build_plan(case(status="APPROVED"), [{"criterion_id": "C", "status": "MATCH_POSSIBLE"}], [], "UNVERIFIED", "MANAGER", NOW, CONFIG)
        self.assertEqual(terminal.verdict, "UNCERTAIN")
        self.assertEqual(terminal.steps, [])
        missing_policy = self.plan(policy="UNKNOWN", role="MANAGER")
        self.assertEqual(missing_policy.verdict, "UNCERTAIN")
        self.assertFalse(any(step.action_type == "RESOLVE_POLICY_VERIFICATION" for step in missing_policy.steps))
        manager_review = build_plan({**case(), "approval_state": "PENDING_MANAGER_APPROVAL"}, [{"criterion_id": "C", "status": "HUMAN_CONFIRMED"}], [], "VERIFIED", "SPECIALIST", NOW, CONFIG)
        self.assertEqual(manager_review.verdict, "BLOCKED_BY_PERMISSION")
        self.assertEqual(manager_review.steps[0].action_type, "REQUEST_MANAGER_REVIEW")

    def test_overdue_missing_timezone_and_invalid_config(self):
        overdue = self.plan(deadline=NOW - timedelta(minutes=1))
        self.assertEqual(overdue.verdict, "INFEASIBLE_WITHOUT_EXTENSION")
        missing = self.plan(deadline=None)
        self.assertEqual(missing.verdict, "UNCERTAIN")
        self.assertEqual(self.plan(deadline=datetime(2026, 10, 24, 12)).verdict, "UNCERTAIN")
        with self.assertRaises(ValueError):
            self.plan(conf={})

    def test_parallel_actions_use_longest_path_not_sum(self):
        config = json.loads(json.dumps(CONFIG))
        config["actions"]["REQUEST_MISSING_DOCUMENT"]["duration_known"] = True
        config["actions"]["REQUEST_MISSING_DOCUMENT"]["external_dependency"] = False
        result = self.plan(states=[{"criterion_id": "A", "status": "MATCH_POSSIBLE"}, {"criterion_id": "B", "status": "MATCH_POSSIBLE"}], conf=config)
        self.assertEqual(result.critical_path_minutes, 5)
        self.assertTrue(all(step.parallelizable for step in result.steps))


class PlannerEndpointTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:", check_same_thread=False)
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.execute("PRAGMA foreign_keys=ON")

        def connect():
            return self.memory_db

        self.connect = connect
        self.patches = [patch.object(module, "connect", connect) for module in (database, main, approval_api, security)]
        self.patches.extend([patch.object(main, "initialize", lambda: None), patch.object(main, "index_policies", lambda: None)])
        for item in self.patches:
            item.start()
        database.initialize()
        self.addCleanup(self._cleanup)
        with connect() as db:
            staff_ids = {row["role"]: row["id"] for row in db.execute("SELECT id,role FROM pa_staff WHERE role IN ('SPECIALIST','SUPERVISOR')")}
            outsider_id = int(db.execute("SELECT COALESCE(MAX(id),0)+1 FROM pa_staff").fetchone()[0])
            salt = os.urandom(16)
            outsider_password = f"pbkdf2_sha256${salt.hex()}${hashlib.pbkdf2_hmac('sha256', b'not-used', salt, 310000).hex()}"
            db.execute("INSERT INTO pa_staff(id,username,display_name,role,password_hash,created_at) VALUES(?, 'outsider','Out of scope','SPECIALIST',?,'now')", (outsider_id, outsider_password))
            for token, staff_id in (("assigned", staff_ids["SPECIALIST"]), ("supervisor", staff_ids["SUPERVISOR"]), ("outsider", outsider_id)):
                db.execute("INSERT INTO pa_sessions(token_hash,staff_id,expires_at) VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), staff_id, (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()))
            db.execute("INSERT INTO cases VALUES('PA-PLAN-1','SYNTHETIC-PLAN','Example Payer','MRI','2026-10-24',?,'NEW','ON TRACK','Test Specialist','MEDIUM')", ((datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),))
            db.execute("INSERT INTO pa_case_assignments(case_id,specialist_id,supervisor_id,updated_at) VALUES('PA-PLAN-1',?,?,'now')", (staff_ids["SPECIALIST"], staff_ids["SUPERVISOR"]))
            db.execute("INSERT INTO policy_documents(policy_id,document_type,title,content,effective_date,version,approved,payer,service_type,verification_status) VALUES('P1','PAYER','Synthetic policy','', '2026-01-01','1',1,'Example Payer','MRI','VERIFIED_PUBLIC')")
            db.execute("INSERT INTO policy_criteria(policy_id,policy_version,criterion_id,criterion_text,verification_status) VALUES('P1','1','C1','Synthetic criterion','VERIFIED_PUBLIC')")
            db.execute("INSERT INTO case_evidence_reviews(case_id,policy_id,policy_version,criterion_id,assessment_status,review_status,reviewer_id,reviewed_at,review_notes,created_at) VALUES('PA-PLAN-1','P1','1','C1','MATCH_POSSIBLE','PENDING',NULL,NULL,'','now')")
        self.client = TestClient(main.app)

    def _cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.memory_db.close()

    def test_auth_scope_read_only_and_audit(self):
        path = "/api/cases/PA-PLAN-1/fastest-safe-path"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={"Authorization": "Bearer supervisor"}).status_code, 200)
        self.assertEqual(self.client.get(path, headers={"Authorization": "Bearer outsider"}).status_code, 404)
        # The assigned specialist can read, but cannot confirm the reviewer-only match.
        before = self.snapshot()
        response = self.client.get(path, headers={"Authorization": "Bearer assigned"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["verdict"], "BLOCKED_BY_PERMISSION")
        after = self.snapshot()
        self.assertEqual(before, after)
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events WHERE action='PLAN_GENERATED'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT actor FROM audit_events WHERE action='PLAN_GENERATED' ORDER BY id DESC LIMIT 1").fetchone()[0], "specialist")

    def test_existing_human_review_changes_the_next_plan(self):
        path = "/api/cases/PA-PLAN-1/fastest-safe-path"
        headers = {"Authorization": "Bearer supervisor"}
        before = self.client.get(path, headers=headers).json()
        self.assertEqual(before["steps"][0]["action_type"], "CONFIRM_EVIDENCE_MATCH")
        review = self.client.post("/api/cases/PA-PLAN-1/evidence-trail/C1/review", headers=headers, json={
            "document_id": None, "assessment_status": "MATCH_POSSIBLE", "review_status": "HUMAN_CONFIRMED",
            "supporting_excerpt": "", "source_locator": "", "review_notes": "Synthetic reviewer transition test",
        })
        self.assertEqual(review.status_code, 200, review.text)
        after = self.client.get(path, headers=headers).json()
        self.assertEqual(after["steps"], [])
        self.assertEqual(after["verdict"], "READY_FOR_HUMAN_SUBMISSION_REVIEW")
        self.assertLess(after["critical_path_minutes"], before["critical_path_minutes"])

    def snapshot(self):
        with self.connect() as db:
            return tuple(db.execute(query).fetchall() for query in (
                "SELECT status FROM cases WHERE id='PA-PLAN-1'",
                "SELECT * FROM pa_case_assignments WHERE case_id='PA-PLAN-1'",
                "SELECT review_status,assessment_status FROM case_evidence_reviews WHERE case_id='PA-PLAN-1'",
                "SELECT verification_status FROM policy_documents WHERE policy_id='P1'",
                "SELECT verification_status FROM policy_criteria WHERE policy_id='P1'",
                "SELECT status,reviewed_by,reviewed_at FROM documents WHERE case_id='PA-PLAN-1'",
                "SELECT * FROM pa_submission_approvals WHERE case_id='PA-PLAN-1'",
                "SELECT status FROM pa_escalations WHERE case_id='PA-PLAN-1'",
            ))


if __name__ == "__main__":
    unittest.main()
