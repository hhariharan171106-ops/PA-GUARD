import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import approval_api
import database
import submission_api


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class SubmissionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "submission.sqlite"

        def connect():
            db = sqlite3.connect(self.db_path, factory=ClosingConnection)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        self.connect_patch = patch.multiple(approval_api, connect=connect)
        self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)
        self.submission_connect_patch = patch.object(submission_api, "connect", connect)
        self.submission_connect_patch.start()
        self.addCleanup(self.submission_connect_patch.stop)
        self.prediction_patch = patch.object(submission_api, "predict_case_sla_risk", return_value={"available": True, "risk_level": "LOW", "risk_score": 0.1})
        self.prediction_patch.start()
        self.addCleanup(self.prediction_patch.stop)
        salt = os.urandom(16)
        password_hash = f"pbkdf2_sha256${salt.hex()}${hashlib.pbkdf2_hmac('sha256', b'PasswordDemo123', salt, 310000).hex()}"
        self.tokens = {"specialist":"specialist-token", "unassigned":"unassigned-token", "supervisor":"supervisor-token", "manager":"manager-token"}
        with connect() as db:
            db.executescript("""
                CREATE TABLE pa_staff(id INTEGER PRIMARY KEY,username TEXT,display_name TEXT,role TEXT,password_hash TEXT,active INTEGER,available INTEGER,max_workload INTEGER,service_types TEXT,created_at TEXT);
                CREATE TABLE pa_sessions(token_hash TEXT PRIMARY KEY,staff_id INTEGER,expires_at TEXT);
                CREATE TABLE cases(id TEXT PRIMARY KEY,patient_reference TEXT,payer TEXT,service TEXT,submission_date TEXT,deadline TEXT,status TEXT,risk TEXT,coordinator TEXT,priority TEXT);
                CREATE TABLE pa_case_assignments(case_id TEXT PRIMARY KEY,specialist_id INTEGER,supervisor_id INTEGER,manager_id INTEGER,updated_at TEXT);
                CREATE TABLE policy_documents(policy_id TEXT,document_type TEXT,title TEXT,effective_date TEXT,expires_at TEXT,version TEXT,approved INTEGER,payer TEXT,service_type TEXT,source_reference TEXT,required_documents TEXT,verification_status TEXT);
                CREATE TABLE policy_criteria(policy_id TEXT,policy_version TEXT,criterion_id TEXT,criterion_text TEXT,verification_status TEXT);
                CREATE TABLE case_evidence_reviews(id INTEGER PRIMARY KEY,case_id TEXT,policy_id TEXT,policy_version TEXT,criterion_id TEXT,assessment_status TEXT,review_status TEXT,reviewer_id TEXT,reviewed_at TEXT,review_notes TEXT,document_id INTEGER,source_locator TEXT,supporting_excerpt TEXT,audit_event_id INTEGER,created_at TEXT);
                CREATE TABLE documents(id INTEGER PRIMARY KEY,case_id TEXT,name TEXT,status TEXT,category TEXT,original_filename TEXT,ocr_status TEXT,processing_status TEXT,extracted_text TEXT,page_text TEXT,uploaded_at TEXT,processed_at TEXT);
                CREATE TABLE workflow_failures(id INTEGER PRIMARY KEY,case_id TEXT);
                CREATE TABLE manual_follow_ups(id INTEGER PRIMARY KEY,case_id TEXT,status TEXT);
                CREATE TABLE sla_settings(id INTEGER PRIMARY KEY,warning_hours INTEGER,urgent_hours INTEGER);
                CREATE TABLE pa_submission_readiness(case_id TEXT PRIMARY KEY,version INTEGER,status TEXT,fingerprint TEXT,decision_json TEXT,evaluated_by INTEGER,evaluated_at TEXT);
                CREATE TABLE pa_submission_approvals(case_id TEXT PRIMARY KEY,readiness_version INTEGER,approved_by INTEGER,reason TEXT,approved_at TEXT);
                CREATE TABLE pa_submission_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,case_id TEXT,idempotency_key TEXT,adapter_type TEXT,status TEXT,reference_id TEXT,response_summary TEXT,attempted_by INTEGER,attempted_at TEXT,UNIQUE(case_id,idempotency_key));
                CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,case_id TEXT,timestamp TEXT,actor TEXT,action TEXT,detail TEXT,decision_type TEXT);
            """)
            staff = [(1,"specialist","Assigned Specialist","SPECIALIST"),(2,"supervisor","Assigned Supervisor","SUPERVISOR"),(3,"manager","Demo Manager","MANAGER"),(4,"unassigned","Unassigned Specialist","SPECIALIST")]
            for staff_id, username, display, role in staff:
                db.execute("INSERT INTO pa_staff VALUES(?,?,?,?,?,1,1,10,'[]','now')", (staff_id,username,display,role,password_hash))
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            for username, token in self.tokens.items():
                staff_id = {"specialist":1,"supervisor":2,"manager":3,"unassigned":4}[username]
                db.execute("INSERT INTO pa_sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(),staff_id,expires))
            deadline = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            db.execute("INSERT INTO cases VALUES('PA-SUB-1','SYNTH-1','Northstar Health','Lumbar MRI',?,?, 'NEW','ON TRACK','Assigned Specialist','MEDIUM')", (date.today().isoformat(),deadline))
            db.execute("INSERT INTO pa_case_assignments VALUES('PA-SUB-1',1,2,3,'now')")
            db.execute("INSERT INTO policy_documents VALUES('REQ-1','PAYER_REQUIREMENT','Northstar MRI checklist','2026-01-01',NULL,'1',1,'Northstar Health','Lumbar MRI','Synthetic test fixture source',?,'VERIFIED_PUBLIC')", (json.dumps(["Clinical notes"]),))
            db.execute("INSERT INTO policy_criteria VALUES('REQ-1','1','CR-TEST-1','Clinical notes','VERIFIED_PUBLIC')")
            text = "Clinical notes describe the synthetic case history, examination, and treatment plan for review."
            db.execute("INSERT INTO documents VALUES(1,'PA-SUB-1','notes.pdf','RECEIVED','Clinical Reports','notes.pdf','COMPLETED','COMPLETED',?,?,'now','now')", (text,json.dumps([text])))
            db.execute("INSERT INTO case_evidence_reviews(case_id,policy_id,policy_version,criterion_id,assessment_status,review_status,reviewer_id,reviewed_at,review_notes,document_id,source_locator,supporting_excerpt,created_at) VALUES('PA-SUB-1','REQ-1','1','CR-TEST-1','MATCH_POSSIBLE','HUMAN_CONFIRMED','supervisor','now','Synthetic test fixture review',1,'page 1',?,'now')", (text,))
        app = FastAPI()
        app.include_router(approval_api.router)
        app.include_router(submission_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.temp.cleanup()

    def headers(self, role):
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def evaluate(self, role="manager"):
        result = self.client.post("/api/cases/PA-SUB-1/submission/readiness", headers=self.headers(role))
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_readiness_ready_missing_and_conflicting_requirements(self):
        ready = self.evaluate()
        self.assertEqual(ready["status"], "READY_FOR_REVIEW")
        self.assertEqual(ready["policy_status"], "VERIFIED_PUBLIC")
        self.assertEqual(ready["evidence_review_status"], "HUMAN_CONFIRMED")
        self.assertEqual(ready["required_evidence"][0]["status"], "FOUND_IN_TEXT")
        self.assertEqual(ready["evidence_provenance"][0]["filename"], "notes.pdf")
        with approval_api.connect() as db:
            db.execute("UPDATE documents SET extracted_text='A synthetic record with unrelated details and no requested document label.',page_text=? WHERE id=1", (json.dumps(["A synthetic record with unrelated details and no requested document label."]),))
        missing = self.evaluate()
        self.assertEqual(missing["status"], "NOT_READY")
        self.assertIn("Clinical notes", missing["missing_items"])
        with approval_api.connect() as db:
            db.execute("UPDATE documents SET extracted_text='Clinical notes describe a synthetic reviewable history for this case.',page_text=? WHERE id=1", (json.dumps(["Clinical notes describe a synthetic reviewable history for this case."]),))
            db.execute("INSERT INTO policy_documents VALUES('REQ-1','PAYER_REQUIREMENT','Conflicting current checklist','2026-01-01',NULL,'2',1,'Northstar Health','Lumbar MRI','Synthetic policy §2',?,'VERIFIED_PUBLIC')", (json.dumps(["Clinical notes"]),))
        conflict = self.evaluate()
        self.assertEqual(conflict["status"], "NEEDS_HUMAN_REVIEW")
        self.assertTrue(conflict["conflicts"])

    def test_demo_only_policy_blocks_readiness_even_when_text_match_exists(self):
        with approval_api.connect() as db:
            db.execute("UPDATE policy_documents SET verification_status='MOCK_DEMO' WHERE policy_id='REQ-1'")
        result = self.evaluate()
        self.assertEqual(result["policy_status"], "UNAVAILABLE_OR_UNVERIFIED")
        self.assertEqual(result["status"], "NEEDS_HUMAN_REVIEW")
        self.assertTrue(any("demonstration-only" in blocker for blocker in result["blockers"]))

    def test_case_assignment_restricts_specialist_and_supervisor_scope(self):
        allowed = self.client.get("/api/cases/PA-SUB-1/submission/readiness", headers=self.headers("specialist"))
        self.assertEqual(allowed.status_code, 200)
        allowed_supervisor = self.client.get("/api/cases/PA-SUB-1/submission/readiness", headers=self.headers("supervisor"))
        self.assertEqual(allowed_supervisor.status_code, 200)
        denied = self.client.get("/api/cases/PA-SUB-1/submission/readiness", headers=self.headers("unassigned"))
        self.assertEqual(denied.status_code, 403)

    def test_only_manager_can_approve_and_demo_submit_is_audited_and_idempotent(self):
        readiness = self.evaluate("specialist")
        version = readiness["version"]
        denied = self.client.post("/api/cases/PA-SUB-1/submission/approve", headers=self.headers("specialist"), json={"readiness_version":version,"reason":"Reviewed and ready for demo"})
        self.assertEqual(denied.status_code, 403)
        approval = self.client.post("/api/cases/PA-SUB-1/submission/approve", headers=self.headers("manager"), json={"readiness_version":version,"reason":"Reviewed synthetic requirements and approved demo"})
        self.assertEqual(approval.status_code, 200, approval.text)
        payload = {"readiness_version":version,"idempotency_key":"stable-retry-key-1"}
        first = self.client.post("/api/cases/PA-SUB-1/submission/submit-demo", headers=self.headers("manager"), json=payload)
        second = self.client.post("/api/cases/PA-SUB-1/submission/submit-demo", headers=self.headers("manager"), json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "SUBMITTED_DEMO")
        self.assertIn("No insurer", first.json()["response_summary"])
        self.assertEqual(first.json()["reference_id"], second.json()["reference_id"])
        with approval_api.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) n FROM pa_submission_attempts").fetchone()["n"], 1)
            actions = {row["action"] for row in db.execute("SELECT action FROM audit_events")}
        self.assertIn("SUBMISSION_APPROVED_FOR_DEMO", actions)
        self.assertIn("DEMO_SUBMISSION_RECORDED", actions)

    def test_requires_approval_and_rejects_stale_readiness(self):
        readiness = self.evaluate()
        version = readiness["version"]
        no_approval = self.client.post("/api/cases/PA-SUB-1/submission/submit-demo", headers=self.headers("manager"), json={"readiness_version":version,"idempotency_key":"stable-retry-key-2"})
        self.assertEqual(no_approval.status_code, 409)
        self.client.post("/api/cases/PA-SUB-1/submission/approve", headers=self.headers("manager"), json={"readiness_version":version,"reason":"Reviewed synthetic requirements and approved demo"})
        with approval_api.connect() as db:
            db.execute("UPDATE documents SET extracted_text='changed source text' WHERE id=1")
        stale = self.client.post("/api/cases/PA-SUB-1/submission/submit-demo", headers=self.headers("manager"), json={"readiness_version":version,"idempotency_key":"stable-retry-key-3"})
        self.assertEqual(stale.status_code, 409)

    def test_failed_demo_attempt_is_recorded_and_retry_uses_new_idempotency_key(self):
        readiness = self.evaluate()
        version = readiness["version"]
        self.client.post("/api/cases/PA-SUB-1/submission/approve", headers=self.headers("manager"), json={"readiness_version":version,"reason":"Reviewed synthetic requirements and approved demo"})
        with patch.object(submission_api, "run_demo_adapter", side_effect=RuntimeError("synthetic failure")):
            failed = self.client.post("/api/cases/PA-SUB-1/submission/submit-demo", headers=self.headers("manager"), json={"readiness_version":version,"idempotency_key":"stable-failed-key"})
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["status"], "FAILED")
        retry = self.client.post("/api/cases/PA-SUB-1/submission/submit-demo", headers=self.headers("manager"), json={"readiness_version":version,"idempotency_key":"new-explicit-retry-key"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["status"], "SUBMITTED_DEMO")
        with approval_api.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) n FROM pa_submission_attempts").fetchone()["n"], 2)

    def test_initialize_adds_submission_tables_without_recreating_database(self):
        migration_db = Path(self.temp.name) / "migration.sqlite"
        storage = Path(self.temp.name) / "documents"
        def migration_connect():
            connection = sqlite3.connect(migration_db, factory=ClosingConnection)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        with patch.object(database, "DB_PATH", migration_db), patch.object(database, "DOCUMENT_STORAGE_DIR", storage), patch.object(database, "connect", migration_connect):
            database.initialize()
        db = sqlite3.connect(migration_db)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        self.assertTrue({"pa_submission_readiness", "pa_submission_approvals", "pa_submission_attempts"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
