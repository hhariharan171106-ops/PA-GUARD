import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.routing import Route

import approval_api
import main
import security


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "security.sqlite"

        def connect():
            db = sqlite3.connect(self.db_path, factory=ClosingConnection)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db

        with connect() as db:
            db.executescript("""
              CREATE TABLE pa_staff(id INTEGER PRIMARY KEY,username TEXT,display_name TEXT,role TEXT,password_hash TEXT,active INTEGER,available INTEGER,max_workload INTEGER,service_types TEXT DEFAULT '[]',created_at TEXT);
              CREATE TABLE pa_sessions(token_hash TEXT PRIMARY KEY,staff_id INTEGER,expires_at TEXT);
              CREATE TABLE cases(id TEXT PRIMARY KEY,patient_reference TEXT,payer TEXT,service TEXT,submission_date TEXT,deadline TEXT,status TEXT,risk TEXT,coordinator TEXT,priority TEXT);
              CREATE TABLE pa_case_assignments(case_id TEXT PRIMARY KEY,specialist_id INTEGER,supervisor_id INTEGER,manager_id INTEGER,updated_at TEXT);
              CREATE TABLE pa_assignment_history(id INTEGER PRIMARY KEY AUTOINCREMENT,case_id TEXT,actor_id INTEGER,from_staff_id INTEGER,to_staff_id INTEGER,role TEXT,reason TEXT,created_at TEXT);
              CREATE TABLE pa_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,case_id TEXT,actor_id INTEGER,action TEXT,reason TEXT,old_status TEXT,new_status TEXT,created_at TEXT);
              CREATE TABLE pa_escalations(id INTEGER PRIMARY KEY AUTOINCREMENT,case_id TEXT,from_staff_id INTEGER,from_role TEXT,to_staff_id INTEGER,to_role TEXT,reason TEXT,explanation TEXT,status TEXT,created_at TEXT,updated_at TEXT);
              CREATE TABLE escalation_role_assignments(id INTEGER PRIMARY KEY,escalation_level TEXT,responsible_role TEXT,department TEXT,effective_date TEXT,expires_at TEXT);
              CREATE TABLE documents(id INTEGER PRIMARY KEY,case_id TEXT,name TEXT,status TEXT);
              CREATE TABLE workflow_failures(id INTEGER PRIMARY KEY,case_id TEXT,occurred_at TEXT,error_code TEXT,error_details TEXT,workflow_state TEXT);
              CREATE TABLE audit_events(id INTEGER PRIMARY KEY,case_id TEXT,timestamp TEXT,actor TEXT,action TEXT,detail TEXT,decision_type TEXT);
              CREATE TABLE sla_settings(id INTEGER PRIMARY KEY,warning_hours INTEGER,urgent_hours INTEGER);
            """)
            password = "SecurityDemo123"
            salt = os.urandom(16)
            hashed = f"pbkdf2_sha256${salt.hex()}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 310000).hex()}"
            for staff_id, username, role in [(1,"spec1","SPECIALIST"),(2,"spec2","SPECIALIST"),(3,"manager","MANAGER"),(4,"admin","ADMIN"),(5,"supervisor","SUPERVISOR"),(6,"spec_unassigned","SPECIALIST")]:
                db.execute("INSERT INTO pa_staff VALUES(?,?,?,?,?,1,1,10,'[]','now')",(staff_id,username,username,role,hashed))
            db.executemany("INSERT INTO escalation_role_assignments VALUES(?,?,?,?,?,NULL)",[(1,"LEVEL_1","Prior Authorization Supervisor","Prior Authorization","2026-01-01"),(2,"LEVEL_2","SLA Manager","Prior Authorization","2026-01-01")])
            db.execute("INSERT INTO cases VALUES('CASE-1','SYNTH-1','Payer','Service','2026-01-01','2099-01-01','NEW','ON TRACK','test','LOW')")
            db.execute("INSERT INTO cases VALUES('CASE-2','SYNTH-2','Payer','Service','2026-01-01','2099-01-01','NEW','ON TRACK','test','LOW')")
            db.execute("INSERT INTO cases VALUES('CASE-3','SYNTH-3','Payer','Service','2026-01-01','2099-01-01','NEW','ON TRACK','test','LOW')")
            db.execute("INSERT INTO pa_case_assignments VALUES('CASE-1',1,5,NULL,'now')")
            db.execute("INSERT INTO pa_case_assignments VALUES('CASE-2',2,5,NULL,'now')")
            db.execute("INSERT INTO documents VALUES(1,'CASE-1','one.pdf','UPLOADED')")
            db.execute("INSERT INTO documents VALUES(2,'CASE-2','two.pdf','UPLOADED')")
            db.execute("INSERT INTO workflow_failures VALUES(1,'CASE-1','2026-10-20','EVIDENCE_MISSING','Synthetic failure for case 1','EVIDENCE')")
            db.execute("INSERT INTO workflow_failures VALUES(2,'CASE-2','2026-10-21','SERVICE_UNAVAILABLE','Synthetic failure for case 2','RECOVERY')")
            db.execute("INSERT INTO sla_settings VALUES(1,24,8)")
        self.patches = [patch.object(module, "connect", connect) for module in (approval_api, main, security)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.password = "SecurityDemo123"

    def tearDown(self):
        self.temp.cleanup()

    def token(self, username):
        response = self.client.post("/api/auth/login", json={"username": username, "password": self.password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def test_route_inventory_has_explicit_public_routes_and_no_unmapped_apis(self):
        observed = set()
        framework_routes = set()
        for route in main.app.routes:
            if not isinstance(route, Route):
                continue
            for method in route.methods or ():
                if method in {"HEAD", "OPTIONS"}:
                    continue
                if not route.path.startswith("/api/"):
                    framework_routes.add((method, route.path))
                    continue
                observed.add((method, route.path))
                if (method, route.path) not in security.PUBLIC_ROUTES:
                    self.assertTrue(security.allowed_roles(method, route.path), f"Unmapped API route: {method} {route.path}")
        self.assertEqual(security.PUBLIC_ROUTES, {("GET", "/api/health"), ("POST", "/api/auth/login")})
        self.assertTrue(observed.issuperset(security.PUBLIC_ROUTES))
        self.assertEqual(framework_routes, {("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc")})

    def test_authentication_role_scope_case_id_and_parent_document_scope(self):
        self.assertEqual(self.client.get("/api/cases").status_code, 401)
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        specialist = self.token("spec1")
        headers = {"Authorization": f"Bearer {specialist}"}
        self.assertEqual([case["id"] for case in self.client.get("/api/cases", headers=headers).json()], ["CASE-1"])
        self.assertEqual(self.client.get("/api/cases/CASE-2", headers=headers).status_code, 404)
        self.assertEqual(self.client.get("/api/documents/2", headers=headers).status_code, 404)
        self.assertEqual(self.client.get("/api/documents/1", headers=headers).status_code, 200)
        admin = self.token("admin")
        self.assertEqual(self.client.get("/api/cases/CASE-1", headers={"Authorization": f"Bearer {admin}"}).status_code, 403)

    def test_case_list_is_scoped_by_role_and_keeps_unassigned_cases_manager_only(self):
        def case_ids(username):
            response = self.client.get(
                "/api/cases",
                headers={"Authorization": f"Bearer {self.token(username)}"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsInstance(response.json(), list)
            return {case["id"] for case in response.json()}

        self.assertEqual(case_ids("spec1"), {"CASE-1"})
        self.assertEqual(case_ids("spec2"), {"CASE-2"})
        self.assertEqual(case_ids("spec_unassigned"), set())
        self.assertEqual(case_ids("supervisor"), {"CASE-1", "CASE-2"})
        self.assertEqual(case_ids("manager"), {"CASE-1", "CASE-2", "CASE-3"})

    def test_manager_can_allocate_and_reassign_cases_atomically_with_history(self):
        manager={"Authorization":f"Bearer {self.token('manager')}"}
        listing=self.client.get("/api/work-allocation/cases",headers=manager)
        self.assertEqual(listing.status_code,200,listing.text)
        self.assertEqual({row["id"] for row in listing.json()},{"CASE-1","CASE-2","CASE-3"})
        allocated=self.client.post("/api/work-allocation/bulk-assign",headers=manager,json={"case_ids":["CASE-3"],"staff_id":6,"reason":"Balanced intake workload"})
        self.assertEqual(allocated.status_code,200,allocated.text)
        specialist={"Authorization":f"Bearer {self.token('spec_unassigned')}"}
        self.assertEqual([row["id"] for row in self.client.get("/api/cases",headers=specialist).json()], ["CASE-3"])
        moved=self.client.post("/api/work-allocation/bulk-assign",headers=manager,json={"case_ids":["CASE-3"],"staff_id":1,"reason":"Specialist coverage change"})
        self.assertEqual(moved.status_code,200,moved.text)
        with main.connect() as db:
            self.assertEqual(db.execute("SELECT specialist_id FROM pa_case_assignments WHERE case_id='CASE-3'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pa_assignment_history WHERE case_id='CASE-3'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events WHERE case_id='CASE-3'").fetchone()[0],2)

    def test_manager_allocation_metrics_come_from_database_and_are_role_scoped(self):
        manager={"Authorization":f"Bearer {self.token('manager')}"}
        response=self.client.get("/api/work-allocation/metrics",headers=manager)
        self.assertEqual(response.status_code,200,response.text)
        metrics=response.json()
        self.assertEqual((metrics["total"],metrics["assigned"],metrics["unassigned"]),(3,2,1))
        self.assertEqual(metrics["pending_escalations"],0)
        self.assertEqual(metrics["by_priority"],{"LOW":3})
        specialist={"Authorization":f"Bearer {self.token('spec1')}"}
        self.assertEqual(self.client.get("/api/work-allocation/metrics",headers=specialist).status_code,403)

    def test_assignment_rejects_wrong_role_capacity_and_unauthorized_user(self):
        specialist={"Authorization":f"Bearer {self.token('spec1')}"}
        denied=self.client.post("/api/work-allocation/bulk-assign",headers=specialist,json={"case_ids":["CASE-3"],"staff_id":6,"reason":"No"})
        self.assertEqual(denied.status_code,403)
        manager={"Authorization":f"Bearer {self.token('manager')}"}
        wrong_role=self.client.post("/api/work-allocation/bulk-assign",headers=manager,json={"case_ids":["CASE-1"],"staff_id":5,"reason":"Wrong queue"})
        self.assertEqual(wrong_role.status_code,400)
        missing=self.client.post("/api/work-allocation/bulk-assign",headers=manager,json={"case_ids":["CASE-3","MISSING"],"staff_id":1,"reason":"Missing case"})
        self.assertEqual(missing.status_code,404)
        with main.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM pa_case_assignments WHERE case_id='MISSING'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM pa_case_assignments WHERE case_id='CASE-3'").fetchone())

    def test_specialist_escalation_routes_to_configured_supervisor_and_audits_it(self):
        specialist={"Authorization":f"Bearer {self.token('spec1')}"}
        response=self.client.post("/api/cases/CASE-1/escalate",headers=specialist,json={"reason":"Payer policy clarification required","explanation":"The applicable coverage pathway needs supervisor review.","to_staff_id":1,"to_role":"ADMIN"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["to_role"],"SUPERVISOR")
        self.assertEqual(response.json()["to_staff_id"],5)
        supervisor={"Authorization":f"Bearer {self.token('supervisor')}"}
        self.assertIn("CASE-1",{row["id"] for row in self.client.get("/api/cases",headers=supervisor).json()})
        pending=self.client.get("/api/escalations/requests",headers=supervisor)
        self.assertEqual(pending.status_code,200,pending.text)
        self.assertEqual(pending.json()[0]["status"],"ROUTED")
        duplicate=self.client.post("/api/cases/CASE-1/escalate",headers=specialist,json={"reason":"Other","explanation":"Repeated escalation."})
        self.assertEqual(duplicate.status_code,409)
        accepted=self.client.post("/api/approvals/CASE-1/action",headers=supervisor,json={"action":"ACKNOWLEDGE_ESCALATION","reason":"Supervisor accepted routing for review"})
        self.assertEqual(accepted.status_code,200,accepted.text)
        self.assertEqual(accepted.json()["status"],"AWAITING_SUPERVISOR")
        history=self.client.get("/api/escalations/requests?status=ACCEPTED",headers=supervisor)
        self.assertEqual(history.status_code,200,history.text)
        self.assertEqual(history.json()[0]["to_staff_name"],"supervisor")
        with main.connect() as db:
            escalation=db.execute("SELECT from_staff_id,from_role,to_staff_id,to_role,status FROM pa_escalations WHERE case_id='CASE-1'").fetchone()
            self.assertEqual(tuple(escalation),(1,"SPECIALIST",5,"SUPERVISOR","ACCEPTED"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pa_assignment_history WHERE case_id='CASE-1'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT action FROM audit_events WHERE case_id='CASE-1'").fetchone()[0],"CASE_ESCALATED")

    def test_escalation_rejects_unassigned_resolved_and_invalid_requests(self):
        specialist={"Authorization":f"Bearer {self.token('spec1')}"}
        missing=self.client.post("/api/cases/NOT-A-CASE/escalate",headers=specialist,json={"reason":"Other","explanation":"No such case."})
        self.assertEqual(missing.status_code,404)
        with main.connect() as db: db.execute("UPDATE cases SET status='APPROVED' WHERE id='CASE-1'")
        resolved=self.client.post("/api/cases/CASE-1/escalate",headers=specialist,json={"reason":"Other","explanation":"Resolved case check."})
        self.assertEqual(resolved.status_code,409)
        invalid=self.client.post("/api/cases/CASE-1/escalate",headers=specialist,json={"reason":"Not a reason","explanation":"Invalid reason check."})
        self.assertEqual(invalid.status_code,422)
        with main.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pa_escalations").fetchone()[0],0)

    def test_active_case_list_hides_final_cases_without_deleting_history(self):
        with main.connect() as db:
            db.execute("UPDATE cases SET status='APPROVED' WHERE id='CASE-1'")
        specialist_headers={"Authorization":f"Bearer {self.token('spec1')}"}
        manager_headers={"Authorization":f"Bearer {self.token('manager')}"}
        self.assertEqual(self.client.get("/api/cases",headers=specialist_headers).json(),[])
        manager_cases=self.client.get("/api/cases",headers=manager_headers).json()
        self.assertNotIn("CASE-1",{row["id"] for row in manager_cases})
        with main.connect() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM cases WHERE id='CASE-1'").fetchone())

    def test_manager_case_edits_are_audited_and_concurrency_guarded(self):
        manager_headers={"Authorization":f"Bearer {self.token('manager')}"}
        updated=self.client.patch("/api/cases/CASE-1",headers=manager_headers,json={
            "payer":"Updated Payer","deadline":"2099-02-03T14:30:00+05:30","priority":"HIGH",
            "reason":"Corrected from authorization notice",
            "expected_values":{"payer":"Payer","deadline":"2099-01-01","priority":"LOW"},
        })
        self.assertEqual(updated.status_code,200,updated.text)
        self.assertEqual(updated.json()["deadline"],"2099-02-03T14:30:00+05:30")
        with main.connect() as db:
            event=db.execute("SELECT actor,action,detail FROM audit_events WHERE case_id='CASE-1'").fetchone()
            self.assertEqual(event["actor"],"manager")
            self.assertEqual(event["action"],"CASE_UPDATED")
            self.assertIn("2099-01-01",event["detail"])
            self.assertIn("2099-02-03T14:30:00+05:30",event["detail"])
        conflict=self.client.patch("/api/cases/CASE-1",headers=manager_headers,json={
            "payer":"Stale overwrite","reason":"Stale form","expected_values":{"payer":"Payer"},
        })
        self.assertEqual(conflict.status_code,409)
        self.assertEqual(conflict.json()["detail"],"Case changed since it was opened; refresh before saving")

    def test_manager_case_edit_accepts_ui_fields_and_audits_all_changes(self):
        manager_headers={"Authorization":f"Bearer {self.token('manager')}"}
        before={"patient_reference":"SYNTH-1","payer":"Payer","service":"Service","priority":"LOW"}
        after={"patient_reference":" SYNTH-1 corrected ","payer":"New Payer","service":"New Service","priority":"HIGH"}
        updated=self.client.patch("/api/cases/CASE-1",headers=manager_headers,json={
            **after,"reason":"Corrected intake details","expected_values":before,
        })
        self.assertEqual(updated.status_code,200,updated.text)
        self.assertEqual(updated.json()["patient_reference"],"SYNTH-1 corrected")
        self.assertEqual(updated.json()["payer"],"New Payer")
        self.assertEqual(updated.json()["service"],"New Service")
        self.assertEqual(updated.json()["priority"],"HIGH")
        with main.connect() as db:
            event=db.execute("SELECT action,detail FROM audit_events WHERE case_id='CASE-1'").fetchone()
            self.assertEqual(event["action"],"CASE_UPDATED")
            for value in (*before.values(),"SYNTH-1 corrected","New Payer","New Service","HIGH","Corrected intake details"):
                self.assertIn(value,event["detail"])

    def test_case_edit_rejects_untrusted_roles_invalid_deadlines_and_mass_assignment(self):
        specialist_headers={"Authorization":f"Bearer {self.token('spec1')}"}
        supervisor_headers={"Authorization":f"Bearer {self.token('supervisor')}"}
        manager_headers={"Authorization":f"Bearer {self.token('manager')}"}
        payload={"deadline":"2099-04-01T10:00:00+00:00","reason":"Correction","expected_values":{"deadline":"2099-01-01"}}
        self.assertEqual(self.client.patch("/api/cases/CASE-1",headers=specialist_headers,json=payload).status_code,403)
        self.assertEqual(self.client.patch("/api/cases/CASE-1",headers=supervisor_headers,json=payload).status_code,403)
        self.assertEqual(self.client.patch("/api/cases/CASE-2",headers=specialist_headers,json=payload).status_code,404)
        invalid=self.client.patch("/api/cases/CASE-1",headers=manager_headers,json={**payload,"deadline":"2099-04-01T10:00:00"})
        self.assertEqual(invalid.status_code,422)
        no_reason=self.client.patch("/api/cases/CASE-1",headers=manager_headers,json={"payer":"Changed","expected_values":{"payer":"Payer"}})
        self.assertEqual(no_reason.status_code,422)
        self.assertEqual(self.client.patch("/api/cases/CASE-1",headers=manager_headers,json={"role":"ADMIN"}).status_code,422)
        self.assertEqual(self.client.patch("/api/cases/CASE-1",headers=manager_headers,json={"status":"APPROVED"}).status_code,422)
        with main.connect() as db:
            self.assertEqual(db.execute("SELECT role FROM pa_staff WHERE username='manager'").fetchone()["role"],"MANAGER")
        denied = self.client.get(
            "/api/cases", headers={"Authorization": f"Bearer {self.token('admin')}"}
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "Your role cannot access this API operation")

    def test_cors_wraps_preflight_and_authentication_errors(self):
        origin = "http://localhost:5173"
        preflight = self.client.options(
            "/api/cases/CASE-1/documents/batch",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers.get("access-control-allow-origin"), origin)

        unauthorized = self.client.post(
            "/api/cases/CASE-1/documents/batch",
            headers={"Origin": origin},
        )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.headers.get("access-control-allow-origin"), origin)

        admin = self.token("admin")
        forbidden = self.client.get(
            "/api/workflow-failures",
            headers={"Origin": origin, "Authorization": f"Bearer {admin}"},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.headers.get("access-control-allow-origin"), origin)

    def test_workflow_failures_are_role_allowed_but_filtered_to_case_assignments(self):
        specialist = self.client.get(
            "/api/workflow-failures",
            headers={"Authorization": f"Bearer {self.token('spec1')}"},
        )
        self.assertEqual(specialist.status_code, 200, specialist.text)
        self.assertEqual([item["case_id"] for item in specialist.json()], ["CASE-1"])

        supervisor = self.client.get(
            "/api/workflow-failures",
            headers={"Authorization": f"Bearer {self.token('supervisor')}"},
        )
        self.assertEqual(supervisor.status_code, 200, supervisor.text)
        self.assertEqual({item["case_id"] for item in supervisor.json()}, {"CASE-1", "CASE-2"})

        manager = self.client.get(
            "/api/workflow-failures",
            headers={"Authorization": f"Bearer {self.token('manager')}"},
        )
        self.assertEqual(manager.status_code, 200, manager.text)
        self.assertEqual({item["case_id"] for item in manager.json()}, {"CASE-1", "CASE-2"})

        admin = self.client.get(
            "/api/workflow-failures",
            headers={"Authorization": f"Bearer {self.token('admin')}"},
        )
        self.assertEqual(admin.status_code, 403)
        self.assertEqual(admin.json()["detail"], "Your role cannot access this API operation")

        unauthenticated = self.client.get("/api/workflow-failures")
        self.assertEqual(unauthenticated.status_code, 401)

    def test_specialist_recovery_route_is_preview_only_and_case_scoped(self):
        specialist_token = self.token("spec1")
        preview_result = {"case_id": "CASE-1", "recovery_status": "RECOMMENDATION_ONLY"}
        with patch.object(main, "run_recovery_rag", return_value=preview_result) as recovery:
            preview = self.client.post(
                "/api/agents/recovery/cases/CASE-1/recover",
                headers={"Authorization": f"Bearer {specialist_token}"},
                json={"actor": "Test specialist", "preview_only": True},
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["recovery"], preview_result)
            recovery.assert_called_once_with("CASE-1", "spec1", None, False, True)

            execute = self.client.post(
                "/api/agents/recovery/cases/CASE-1/recover",
                headers={"Authorization": f"Bearer {specialist_token}"},
                json={"actor": "Test specialist", "approve_recovery": True},
            )
            self.assertEqual(execute.status_code, 403)
            self.assertEqual(recovery.call_count, 1)

        out_of_scope = self.client.post(
            "/api/agents/recovery/cases/CASE-2/recover",
            headers={"Authorization": f"Bearer {specialist_token}"},
            json={"actor": "Test specialist", "preview_only": True},
        )
        self.assertEqual(out_of_scope.status_code, 404)

    def test_deactivation_and_role_change_revoke_session_immediately(self):
        token = self.token("spec1")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with approval_api.connect() as db:
            db.execute("UPDATE pa_staff SET active=0 WHERE id=1")
        self.assertEqual(self.client.get("/api/cases", headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        with approval_api.connect() as db:
            db.execute("UPDATE pa_staff SET active=1,role='ADMIN' WHERE id=1")
        self.assertEqual(self.client.get("/api/cases", headers={"Authorization": f"Bearer {token}"}).status_code, 403)
        with approval_api.connect() as db:
            self.assertTrue(db.execute("SELECT 1 FROM pa_sessions WHERE token_hash=?",(token_hash,)).fetchone())


if __name__ == "__main__":
    unittest.main()
