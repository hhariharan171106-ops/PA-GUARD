import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import approval_api


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class ApprovalApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "approvals.sqlite"

        def connect():
            db = sqlite3.connect(self.db_path, factory=ClosingConnection)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        self.connect_patch = patch.object(approval_api, "connect", connect)
        self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)
        password = "PasswordDemo123"
        salt = os.urandom(16)
        hashed = f"pbkdf2_sha256${salt.hex()}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 310000).hex()}"
        with connect() as db:
            db.executescript("""
                CREATE TABLE pa_staff(id INTEGER PRIMARY KEY,username TEXT UNIQUE,display_name TEXT,role TEXT,password_hash TEXT,active INTEGER,available INTEGER,max_workload INTEGER,service_types TEXT DEFAULT '[]',created_at TEXT);
                CREATE TABLE pa_sessions(token_hash TEXT PRIMARY KEY,staff_id INTEGER,expires_at TEXT);
                CREATE TABLE cases(id TEXT PRIMARY KEY,patient_reference TEXT,payer TEXT,service TEXT,submission_date TEXT,deadline TEXT,status TEXT,risk TEXT,coordinator TEXT,priority TEXT);
                CREATE TABLE documents(id INTEGER PRIMARY KEY,case_id TEXT,name TEXT,status TEXT,category TEXT,ocr_status TEXT,processing_status TEXT,processing_error TEXT,evidence_summary TEXT);
                CREATE TABLE sla_settings(id INTEGER PRIMARY KEY,warning_hours INTEGER,urgent_hours INTEGER);
                CREATE TABLE pa_case_assignments(case_id TEXT PRIMARY KEY,specialist_id INTEGER,supervisor_id INTEGER,manager_id INTEGER,updated_at TEXT);
                CREATE TABLE pa_reviews(id INTEGER PRIMARY KEY,case_id TEXT,actor_id INTEGER,action TEXT,reason TEXT,old_status TEXT,new_status TEXT,created_at TEXT);
                CREATE TABLE pa_notes(id INTEGER PRIMARY KEY,case_id TEXT,actor_id INTEGER,role TEXT,note TEXT,created_at TEXT);
                CREATE TABLE pa_assignment_history(id INTEGER PRIMARY KEY,case_id TEXT,actor_id INTEGER,from_staff_id INTEGER,to_staff_id INTEGER,role TEXT,reason TEXT,created_at TEXT);
                CREATE TABLE pa_escalations(id INTEGER PRIMARY KEY,case_id TEXT,from_staff_id INTEGER,from_role TEXT,to_staff_id INTEGER,to_role TEXT,reason TEXT,explanation TEXT,status TEXT,created_at TEXT,updated_at TEXT);
                CREATE TABLE pa_approval_policy(id INTEGER PRIMARY KEY,manager_final_required INTEGER);
                CREATE TABLE audit_events(id INTEGER PRIMARY KEY,case_id TEXT,timestamp TEXT,actor TEXT,action TEXT,detail TEXT,decision_type TEXT);
                CREATE TABLE evidence_recommendations(id INTEGER PRIMARY KEY,case_id TEXT,created_at TEXT,verification_status TEXT,required_documents TEXT,missing_documents TEXT,possible_mismatches TEXT,explanation TEXT,recommended_action TEXT,needs_human_review INTEGER);
                CREATE TABLE sla_recommendations(id INTEGER PRIMARY KEY,case_id TEXT,created_at TEXT,risk_level TEXT,remaining_hours REAL,explanation TEXT,recommended_action TEXT,assigned_role TEXT,confidence REAL,needs_human_review INTEGER);
                CREATE TABLE escalation_recommendations(id INTEGER PRIMARY KEY,case_id TEXT,created_at TEXT,escalation_required INTEGER,escalation_level TEXT,reason TEXT,responsible_role TEXT,recommended_action TEXT,notification_status TEXT);
                CREATE TABLE workflow_failures(id INTEGER PRIMARY KEY,case_id TEXT,occurred_at TEXT,error_code TEXT,error_details TEXT,workflow_state TEXT);
                CREATE TABLE manual_follow_ups(id INTEGER PRIMARY KEY,case_id TEXT,created_at TEXT,responsible_role TEXT,reason TEXT,status TEXT);
                CREATE TABLE pa_staff_history(id INTEGER PRIMARY KEY,staff_id INTEGER,actor_id INTEGER,action TEXT,detail TEXT,created_at TEXT);
            """)
            db.execute("INSERT INTO pa_staff VALUES(1,'specialist','Demo Specialist','SPECIALIST',?,1,1,10,'[]','now')",(hashed,))
            db.execute("INSERT INTO pa_staff VALUES(2,'supervisor','Demo Supervisor','SUPERVISOR',?,1,1,10,'[]','now')",(hashed,))
            db.execute("INSERT INTO pa_staff VALUES(3,'manager','Demo Manager','MANAGER',?,1,1,10,'[]','now')",(hashed,))
            db.execute("INSERT INTO pa_staff VALUES(4,'admin','Demo Admin','ADMIN',?,1,1,10,'[]','now')",(hashed,))
            db.execute("INSERT INTO cases VALUES('PA-TEST','SYNTH','Payer','Service','2026-01-01','2099-01-01T00:00:00+00:00','AWAITING_MANAGER','ON TRACK','Demo','LOW')")
            db.execute("INSERT INTO pa_case_assignments VALUES('PA-TEST',1,NULL,2,'before')")
            db.execute("INSERT INTO documents VALUES(1,'PA-TEST','synthetic.pdf','VERIFIED','Clinical Notes','COMPLETED','COMPLETED',NULL,NULL)")
            db.execute("INSERT INTO pa_approval_policy VALUES(1,1)")
            db.execute("INSERT INTO sla_settings VALUES(1,24,8)")
        app = FastAPI()
        app.include_router(approval_api.router)
        self.client = TestClient(app)
        self.prediction_patch = patch.object(approval_api, "predict_case_sla_risk", return_value={"available": False})
        self.prediction_patch.start()
        self.addCleanup(self.prediction_patch.stop)
        self.password = password

    def tearDown(self):
        self.temp.cleanup()

    def token(self, username):
        response = self.client.post("/api/auth/login", json={"username": username, "password": self.password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def test_role_cannot_be_changed_by_request_and_manager_decision_is_audited(self):
        specialist = self.token("specialist")
        forbidden = self.client.post("/api/approvals/PA-TEST/action", headers={"Authorization": f"Bearer {specialist}"}, json={"action":"APPROVED","reason":"not allowed"})
        self.assertEqual(forbidden.status_code, 403)

        manager = self.token("manager")
        saved = self.client.post("/api/approvals/PA-TEST/action", headers={"Authorization": f"Bearer {manager}"}, json={"action":"APPROVED","reason":"Prototype review complete"})
        self.assertEqual(saved.status_code, 200, saved.text)
        with approval_api.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM cases WHERE id='PA-TEST'").fetchone()["status"], "APPROVED")
            self.assertEqual(db.execute("SELECT COUNT(*) n FROM audit_events WHERE action='APPROVED'").fetchone()["n"], 1)
            review = db.execute("SELECT actor_id,action,reason,old_status,new_status,created_at FROM pa_reviews WHERE case_id='PA-TEST'").fetchone()
            self.assertEqual((review["actor_id"], review["action"], review["reason"], review["old_status"], review["new_status"]), (3, "APPROVED", "Prototype review complete", "AWAITING_MANAGER", "APPROVED"))
            self.assertTrue(review["created_at"])
            event = db.execute("SELECT actor,detail FROM audit_events WHERE case_id='PA-TEST' AND action='APPROVED'").fetchone()
            self.assertEqual(event["actor"], "Demo Manager")
            self.assertEqual(json.loads(event["detail"]), {"reason": "Prototype review complete", "previous_state": "AWAITING_MANAGER", "resulting_state": "APPROVED"})
        manager_headers={"Authorization":f"Bearer {manager}"}
        self.assertEqual(self.client.get("/api/approvals",headers=manager_headers).json()["total_items"],0)
        historical=self.client.get("/api/approvals?status=APPROVED",headers=manager_headers)
        self.assertEqual(historical.status_code,200,historical.text)
        self.assertEqual([row["id"] for row in historical.json()["items"]],["PA-TEST"])
        historical_detail=self.client.get("/api/approvals/PA-TEST",headers=manager_headers)
        self.assertEqual(historical_detail.status_code,200)
        self.assertEqual([document["id"] for document in historical_detail.json()["documents"]],[1])
        with approval_api.connect() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM documents WHERE id=1 AND case_id='PA-TEST'").fetchone())
        duplicate = self.client.post("/api/approvals/PA-TEST/action", headers={"Authorization": f"Bearer {manager}"}, json={"action":"DENIED","reason":"second decision"})
        self.assertEqual(duplicate.status_code, 409)
        with approval_api.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pa_reviews WHERE case_id='PA-TEST'").fetchone()[0], 1)

    def test_each_role_can_login_and_identity_comes_from_me_endpoint(self):
        expected={"specialist":"SPECIALIST","supervisor":"SUPERVISOR","manager":"MANAGER","admin":"ADMIN"}
        for username,role in expected.items():
            token=self.token(username)
            identity=self.client.get("/api/auth/me",headers={"Authorization":f"Bearer {token}"})
            self.assertEqual(identity.status_code,200)
            self.assertEqual(identity.json()["role"],role)
            approval_queue=self.client.get("/api/approvals",headers={"Authorization":f"Bearer {token}"})
            self.assertEqual(approval_queue.status_code,403 if role=="ADMIN" else 200,approval_queue.text)
        bad=self.client.post("/api/auth/login",json={"username":"manager","password":"wrong"})
        self.assertEqual(bad.status_code,401)

    def test_invalid_expired_and_logged_out_sessions_are_rejected(self):
        invalid=self.client.get("/api/auth/me",headers={"Authorization":"Bearer invalid-token"})
        self.assertEqual(invalid.status_code,401)
        manager=self.token("manager")
        with approval_api.connect() as db:
            db.execute("UPDATE pa_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE staff_id=3")
        expired=self.client.get("/api/auth/me",headers={"Authorization":f"Bearer {manager}"})
        self.assertEqual(expired.status_code,401)
        fresh=self.token("manager")
        logged_out=self.client.post("/api/auth/logout",headers={"Authorization":f"Bearer {fresh}"})
        self.assertEqual(logged_out.status_code,200)
        after_logout=self.client.get("/api/auth/me",headers={"Authorization":f"Bearer {fresh}"})
        self.assertEqual(after_logout.status_code,401)

    def test_inactive_account_cannot_log_in_and_reason_is_required(self):
        with approval_api.connect() as db:
            db.execute("UPDATE pa_staff SET active=0 WHERE username='specialist'")
        denied = self.client.post("/api/auth/login", json={"username":"specialist","password":self.password})
        self.assertEqual(denied.status_code, 401)
        manager = self.token("manager")
        missing_reason = self.client.post("/api/approvals/PA-TEST/action", headers={"Authorization":f"Bearer {manager}"}, json={"action":"APPROVED","reason":""})
        self.assertEqual(missing_reason.status_code, 422)
        blank_reason = self.client.post("/api/approvals/PA-TEST/action", headers={"Authorization":f"Bearer {manager}"}, json={"action":"APPROVED","reason":"   "})
        self.assertEqual(blank_reason.status_code, 422)

    def test_allocator_uses_model_priority_and_records_reason(self):
        with approval_api.connect() as db:
            db.execute("INSERT INTO cases VALUES('PA-ALLOC','SYNTH','Payer','Service','2026-01-01','2099-01-01T00:00:00+00:00','NEW','ON TRACK','Demo','LOW')")
        manager = self.token("manager")
        prediction={"available":True,"predicted_sla_breach":True,"risk_score":78.0}
        with patch.object(approval_api,"predict_case_sla_risk",return_value=prediction):
            result=self.client.post("/api/work-allocation/run",headers={"Authorization":f"Bearer {manager}"})
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn("PA-ALLOC",result.json()["assigned_case_ids"])
        with approval_api.connect() as db:
            row=db.execute("SELECT a.specialist_id,h.reason FROM pa_case_assignments a JOIN pa_assignment_history h ON h.case_id=a.case_id WHERE a.case_id='PA-ALLOC'").fetchone()
            self.assertEqual(row["specialist_id"],1)
            self.assertIn("predicted elevated breach risk",row["reason"])

    def test_specialist_submit_routes_to_supervisor_and_preserves_case_status_transition(self):
        with approval_api.connect() as db:
            db.execute("INSERT INTO cases VALUES('PA-SUBMIT','SYNTH-2','Payer','Service','2026-01-01','2099-01-01T00:00:00+00:00','NEW','ON TRACK','Demo','LOW')")
            db.execute("INSERT INTO pa_case_assignments VALUES('PA-SUBMIT',1,NULL,NULL,'before')")
        specialist=self.token("specialist")
        response=self.client.post("/api/approvals/PA-SUBMIT/action",headers={"Authorization":f"Bearer {specialist}"},json={"action":"SUBMIT","reason":"Specialist review completed"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["status"],"AWAITING_SUPERVISOR")
        with approval_api.connect() as db:
            assigned=db.execute("SELECT supervisor_id FROM pa_case_assignments WHERE case_id='PA-SUBMIT'").fetchone()[0]
            self.assertEqual(assigned,2)
            self.assertEqual(db.execute("SELECT role FROM pa_assignment_history WHERE case_id='PA-SUBMIT'").fetchone()[0],"SUPERVISOR")

    def test_supervisor_approval_routes_to_manager_when_manager_final_review_is_required(self):
        with approval_api.connect() as db:
            db.execute("INSERT INTO cases VALUES('PA-SUPERVISOR-APPROVAL','SYNTH-3','Payer','Service','2026-01-01','2099-01-01T00:00:00+00:00','AWAITING_SUPERVISOR','ON TRACK','Demo','LOW')")
            db.execute("INSERT INTO pa_case_assignments VALUES('PA-SUPERVISOR-APPROVAL',1,2,NULL,'before')")
        supervisor=self.token("supervisor")
        response=self.client.post("/api/approvals/PA-SUPERVISOR-APPROVAL/action",headers={"Authorization":f"Bearer {supervisor}"},json={"action":"SUPERVISOR_APPROVE","reason":"Supervisor review complete"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["status"],"AWAITING_MANAGER")
        with approval_api.connect() as db:
            assigned=db.execute("SELECT manager_id FROM pa_case_assignments WHERE case_id='PA-SUPERVISOR-APPROVAL'").fetchone()[0]
            self.assertEqual(assigned,3)
            self.assertEqual(db.execute("SELECT role FROM pa_assignment_history WHERE case_id='PA-SUPERVISOR-APPROVAL'").fetchone()[0],"MANAGER")

    def test_supervisor_handoff_state_matches_manager_review_queue_and_preserves_reason(self):
        with approval_api.connect() as db:
            db.execute("INSERT INTO cases VALUES('PA-MANAGER-HANDOFF','SYNTH-4','Payer','Service','2026-01-01','2099-01-01T00:00:00+00:00','AWAITING_SUPERVISOR','ON TRACK','Demo','LOW')")
            db.execute("INSERT INTO pa_case_assignments VALUES('PA-MANAGER-HANDOFF',1,2,3,'before')")
        supervisor = self.token("supervisor")
        reason = "Supervisor review completed; manager decision required"
        handoff = self.client.post("/api/approvals/PA-MANAGER-HANDOFF/action", headers={"Authorization": f"Bearer {supervisor}"}, json={"action":"FORWARD","reason":reason})
        self.assertEqual(handoff.status_code, 200, handoff.text)
        self.assertEqual((handoff.json()["old_status"], handoff.json()["status"]), ("AWAITING_SUPERVISOR", "AWAITING_MANAGER"))
        manager = self.token("manager")
        queued = self.client.get("/api/approvals?q=PA-MANAGER-HANDOFF", headers={"Authorization": f"Bearer {manager}"})
        self.assertEqual(queued.status_code, 200, queued.text)
        self.assertEqual([(row["id"], row["status"]) for row in queued.json()["items"]], [("PA-MANAGER-HANDOFF", "AWAITING_MANAGER")])
        with approval_api.connect() as db:
            review = db.execute("SELECT actor_id,action,reason,old_status,new_status,created_at FROM pa_reviews WHERE case_id='PA-MANAGER-HANDOFF'").fetchone()
            self.assertEqual((review["actor_id"], review["action"], review["reason"], review["old_status"], review["new_status"]), (2, "FORWARD", reason, "AWAITING_SUPERVISOR", "AWAITING_MANAGER"))
            self.assertTrue(review["created_at"])
            audit = db.execute("SELECT actor,detail FROM audit_events WHERE case_id='PA-MANAGER-HANDOFF' AND action='FORWARD'").fetchone()
            self.assertEqual(audit["actor"], "Demo Supervisor")
            self.assertEqual(json.loads(audit["detail"])["reason"], reason)
            routed = db.execute("SELECT role,reason,created_at FROM pa_assignment_history WHERE case_id='PA-MANAGER-HANDOFF'").fetchone()
            self.assertEqual(routed["role"], "MANAGER")
            self.assertTrue(routed["created_at"])

    def test_manager_queue_and_action_reject_new_cases_until_handoff(self):
        with approval_api.connect() as db:
            db.execute("INSERT INTO cases VALUES('PA-NEW-NOT-REVIEWABLE','SYNTH-5','Payer','Service','2026-01-01','2099-01-01T00:00:00+00:00','NEW','ON TRACK','Demo','LOW')")
            db.execute("INSERT INTO pa_case_assignments VALUES('PA-NEW-NOT-REVIEWABLE',1,2,3,'before')")
        manager = self.token("manager")
        headers = {"Authorization": f"Bearer {manager}"}
        default_queue = self.client.get("/api/approvals", headers=headers)
        self.assertNotIn("PA-NEW-NOT-REVIEWABLE", {row["id"] for row in default_queue.json()["items"]})
        explicit_new_filter = self.client.get("/api/approvals?status=NEW", headers=headers)
        self.assertNotIn("PA-NEW-NOT-REVIEWABLE", {row["id"] for row in explicit_new_filter.json()["items"]})
        decision = self.client.post("/api/approvals/PA-NEW-NOT-REVIEWABLE/action", headers=headers, json={"action":"APPROVED","reason":"Not legitimately handed off"})
        self.assertEqual(decision.status_code, 409)
        self.assertIn("invalid from NEW", decision.json()["detail"])
        with approval_api.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM cases WHERE id='PA-NEW-NOT-REVIEWABLE'").fetchone()[0], "NEW")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pa_reviews WHERE case_id='PA-NEW-NOT-REVIEWABLE'").fetchone()[0], 0)

    def test_manager_return_for_correction_records_reason_and_transition(self):
        manager = self.token("manager")
        reason = "Supervisor must resolve the discrepancy"
        returned = self.client.post("/api/approvals/PA-TEST/action", headers={"Authorization": f"Bearer {manager}"}, json={"action":"RETURN_SUPERVISOR","reason":reason})
        self.assertEqual(returned.status_code, 200, returned.text)
        self.assertEqual((returned.json()["old_status"], returned.json()["status"]), ("AWAITING_MANAGER", "AWAITING_SUPERVISOR"))
        with approval_api.connect() as db:
            review = db.execute("SELECT actor_id,reason,old_status,new_status,created_at FROM pa_reviews WHERE case_id='PA-TEST'").fetchone()
            self.assertEqual((review["actor_id"], review["reason"], review["old_status"], review["new_status"]), (3, reason, "AWAITING_MANAGER", "AWAITING_SUPERVISOR"))
            self.assertTrue(review["created_at"])
            event = db.execute("SELECT actor,detail FROM audit_events WHERE case_id='PA-TEST' AND action='RETURN_SUPERVISOR'").fetchone()
            self.assertEqual(event["actor"], "Demo Manager")
            self.assertEqual(json.loads(event["detail"])["resulting_state"], "AWAITING_SUPERVISOR")

    def test_supervisor_cannot_perform_manager_only_approval(self):
        with approval_api.connect() as db:
            db.execute("UPDATE pa_case_assignments SET supervisor_id=2 WHERE case_id='PA-TEST'")
        supervisor = self.token("supervisor")
        response = self.client.post("/api/approvals/PA-TEST/action", headers={"Authorization": f"Bearer {supervisor}"}, json={"action":"APPROVED","reason":"Attempt manager approval"})
        self.assertEqual(response.status_code, 403)

    def test_approval_search_combined_filters_sort_and_pagination(self):
        manager = self.token("manager")
        headers = {"Authorization": f"Bearer {manager}"}
        with approval_api.connect() as db:
            db.execute("INSERT INTO cases VALUES('PA-SECOND','SYNTH-2','Other Payer','Other Service','2026-02-01','2099-02-01T00:00:00+00:00','APPROVED','ON TRACK','Demo','LOW')")
        found = self.client.get("/api/approvals?q=Payer&service=Service&status=AWAITING_MANAGER&assigned_staff_id=1&sla_status=ON TRACK&submission_from=2026-01-01&deadline_to=2099-12-31&page=1&page_size=20", headers=headers)
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["total_items"], 1)
        self.assertEqual(found.json()["items"][0]["id"], "PA-TEST")
        self.assertIn("ml_prediction_available", found.json()["items"][0])
        self.assertEqual(found.json()["items"][0]["evidence_status"], None)
        self.assertEqual(self.client.get("/api/approvals?q=no-match", headers=headers).json()["total_items"], 0)
        page_two = self.client.get("/api/approvals?page=2&page_size=1&sort_by=case_id&sort_order=desc", headers=headers)
        self.assertEqual(page_two.status_code, 200, page_two.text)
        self.assertEqual(page_two.json()["total_items"], 1)
        self.assertEqual(page_two.json()["total_pages"], 1)
        historical_page = self.client.get("/api/approvals?status=APPROVED", headers=headers)
        self.assertEqual(historical_page.json()["total_items"], 1)
        self.assertEqual(historical_page.json()["items"][0]["id"], "PA-SECOND")
        unavailable = self.client.get("/api/approvals?ml_risk=UNAVAILABLE", headers=headers)
        self.assertEqual(unavailable.json()["total_items"], 1)
        self.assertEqual(self.client.get("/api/approvals?sort_by=arbitrary", headers=headers).status_code, 422)
        self.assertEqual(self.client.get("/api/approvals?page_size=101", headers=headers).status_code, 422)
        specialist = self.token("specialist")
        scoped = self.client.get("/api/approvals", headers={"Authorization": f"Bearer {specialist}"})
        self.assertEqual(scoped.status_code, 200, scoped.text)
        self.assertEqual(scoped.json()["total_items"], 0)
        with approval_api.connect() as db:
            db.execute("INSERT INTO cases VALUES('PA-OTHER','SYNTH','Other Payer','Other Service','2026-01-02','2099-01-01T00:00:00+00:00','NEW','ON TRACK','Demo','LOW')")
            db.execute("INSERT INTO pa_case_assignments VALUES('PA-OTHER',2,NULL,NULL,'now')")
        altered_id = self.client.get("/api/approvals/PA-OTHER", headers={"Authorization": f"Bearer {specialist}"})
        self.assertEqual(altered_id.status_code, 403)

    def test_staff_creation_duplicate_validation_and_safe_response(self):
        admin = self.token("admin")
        headers = {"Authorization": f"Bearer {admin}"}
        payload = {"username":"new-specialist","display_name":"New Specialist","password":"NewAccountPass123","role":"SPECIALIST","active":True,"available":True,"max_workload":12,"service_types":["Imaging"]}
        created = self.client.post("/api/staff", headers=headers, json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn("password", created.json())
        self.assertNotIn("password_hash", created.json())
        self.assertEqual(self.client.post("/api/staff", headers=headers, json=payload).status_code, 409)
        self.assertEqual(self.client.post("/api/staff", headers=headers, json={**payload,"username":"invalid-role","role":"OWNER"}).status_code, 400)
        with approval_api.connect() as db:
            row = db.execute("SELECT password_hash FROM pa_staff WHERE username='new-specialist'").fetchone()
            self.assertNotEqual(row["password_hash"], payload["password"])
            self.assertTrue(row["password_hash"].startswith("pbkdf2_sha256$"))
            staff_id = db.execute("SELECT id FROM pa_staff WHERE username='new-specialist'").fetchone()["id"]
            db.execute("UPDATE pa_case_assignments SET specialist_id=? WHERE case_id='PA-TEST'",(staff_id,))
            self.assertNotIn(payload["password"], db.execute("SELECT detail FROM audit_events WHERE action='STAFF_ACCOUNT_CREATED'").fetchone()["detail"])
        member_login = self.client.post("/api/auth/login",json={"username":"new-specialist","password":payload["password"]})
        self.assertEqual(member_login.status_code,200,member_login.text)
        member_token = member_login.json()["token"]
        updated = self.client.patch(f"/api/staff/{staff_id}",headers=headers,json={"display_name":"New Specialist","role":"MANAGER","active":False,"available":False,"max_workload":5,"service_types":["Imaging"]})
        self.assertEqual(updated.status_code,200,updated.text)
        self.assertEqual(self.client.get("/api/auth/me",headers={"Authorization":f"Bearer {member_token}"}).status_code,401)
        self.assertEqual(self.client.post("/api/auth/login",json={"username":"new-specialist","password":payload["password"]}).status_code,401)
        with approval_api.connect() as db:
            self.assertEqual(db.execute("SELECT specialist_id FROM pa_case_assignments WHERE case_id='PA-TEST'").fetchone()["specialist_id"],staff_id)


if __name__ == "__main__":
    unittest.main()
