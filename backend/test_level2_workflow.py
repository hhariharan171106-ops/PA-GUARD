import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import approval_api
import main
import security


class LevelTwoWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE pa_staff(id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, role TEXT,
                active INTEGER, available INTEGER, max_workload INTEGER, service_types TEXT);
            CREATE TABLE pa_sessions(token_hash TEXT PRIMARY KEY, staff_id INTEGER, expires_at TEXT);
            CREATE TABLE cases(id TEXT PRIMARY KEY, patient_reference TEXT, payer TEXT, service TEXT,
                submission_date TEXT, deadline TEXT, status TEXT, risk TEXT, coordinator TEXT, priority TEXT);
            CREATE TABLE pa_case_assignments(case_id TEXT PRIMARY KEY, specialist_id INTEGER,
                supervisor_id INTEGER, manager_id INTEGER, updated_at TEXT);
            CREATE TABLE pa_escalations(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT,
                from_staff_id INTEGER, from_role TEXT, to_staff_id INTEGER, to_role TEXT, reason TEXT,
                explanation TEXT, status TEXT CHECK(status IN ('ROUTED','ACCEPTED','CLOSED')),
                created_at TEXT, updated_at TEXT);
            CREATE TABLE pa_assignment_history(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT,
                actor_id INTEGER, from_staff_id INTEGER, to_staff_id INTEGER, role TEXT, reason TEXT, created_at TEXT);
            CREATE TABLE pa_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, actor_id INTEGER,
                action TEXT, reason TEXT, old_status TEXT, new_status TEXT, created_at TEXT);
            CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, timestamp TEXT,
                actor TEXT, action TEXT, detail TEXT, decision_type TEXT);
            CREATE TABLE sla_settings(id INTEGER PRIMARY KEY, warning_hours INTEGER);
            CREATE TABLE documents(id INTEGER PRIMARY KEY, case_id TEXT, status TEXT);
            CREATE TABLE evidence_recommendations(id INTEGER PRIMARY KEY, case_id TEXT, verification_status TEXT);
        ''')
        staff = [
            (1, 'specialist', 'Demo Specialist', 'SPECIALIST', 1, 1, 8, '[]'),
            (2, 'out-of-scope', 'Out of Scope Supervisor', 'SUPERVISOR', 1, 1, 8, json.dumps(['Other service'])),
            (3, 'supervisor-one', 'Available Supervisor', 'SUPERVISOR', 1, 1, 8, '[]'),
            (4, 'unavailable', 'Unavailable Supervisor', 'SUPERVISOR', 1, 0, 8, '[]'),
            (5, 'manager', 'Demo Manager', 'MANAGER', 1, 1, 8, '[]'),
            (6, 'supervisor-two', 'Second Supervisor', 'SUPERVISOR', 1, 1, 8, '[]'),
        ]
        for row in staff:
            self.db.execute('INSERT INTO pa_staff VALUES(?,?,?,?,?,?,?,?)', row)
            token = f'level-two-{row[1]}'
            self.db.execute('INSERT INTO pa_sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), row[0], '9999-12-31T23:59:59+00:00'))
        self.db.execute(
            'INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('CASE-DEMO-1', 'SYNTH-1', 'Demo payer', 'Lumbar MRI', '2026-01-01', '2099-01-01', 'NEW', 'ON TRACK', 'Demo Specialist', 'MEDIUM'),
        )
        self.db.execute('INSERT INTO pa_case_assignments VALUES(?,?,?,?,?)', ('CASE-DEMO-1', 1, None, None, '2026-01-01T00:00:00+00:00'))
        self.db.execute('INSERT INTO sla_settings VALUES(1,24)')
        self.db.commit()
        self.patches = [patch.object(module, 'connect', return_value=self.db) for module in (main, approval_api, security)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)

    def tearDown(self):
        self.db.close()

    @staticmethod
    def headers(username):
        return {'Authorization': f'Bearer level-two-{username}'}

    def test_specialist_escalation_enters_manager_allocation_and_supervisor_queue(self):
        specialist = self.headers('specialist')
        escalation = self.client.post(
            '/api/cases/CASE-DEMO-1/escalate', headers=specialist,
            json={'reason': 'Supervisor review required', 'explanation': 'The case requires a supervisor review.'},
        )
        self.assertEqual(escalation.status_code, 200, escalation.text)
        self.assertEqual(escalation.json()['to_role'], 'SUPERVISOR')
        self.assertEqual(escalation.json()['case_status'], 'AWAITING_SUPERVISOR')
        # Out-of-scope and unavailable supervisors are skipped in favor of the eligible role.
        self.assertEqual(escalation.json()['to_staff_id'], 3)

        manager = self.headers('manager')
        queue = self.client.get('/api/work-allocation/cases', headers=manager)
        self.assertEqual(queue.status_code, 200, queue.text)
        case = next(item for item in queue.json() if item['id'] == 'CASE-DEMO-1')
        self.assertEqual(case['status'], 'AWAITING_SUPERVISOR')
        self.assertEqual(case['supervisor_id'], 3)
        staff = self.client.get('/api/staff', headers=manager)
        self.assertEqual(staff.status_code, 200, staff.text)

        assignment = self.client.post(
            '/api/work-allocation/CASE-DEMO-1/assign', headers=manager,
            json={'staff_id': 6, 'reason': 'Balance supervisor workload'},
        )
        self.assertEqual(assignment.status_code, 200, assignment.text)
        self.assertEqual(assignment.json()['case_id'], 'CASE-DEMO-1')
        self.assertEqual(assignment.json()['staff_id'], 6)
        self.assertEqual(assignment.json()['role'], 'SUPERVISOR')

        supervisor = self.headers('supervisor-two')
        cases = self.client.get('/api/cases', headers=supervisor)
        self.assertEqual(cases.status_code, 200, cases.text)
        self.assertIn('CASE-DEMO-1', {item['id'] for item in cases.json()})
        with patch.object(approval_api, 'predict_case_sla_risk', return_value={'available': False}):
            approvals = self.client.get('/api/approvals', headers=supervisor)
        self.assertEqual(approvals.status_code, 200, approvals.text)
        self.assertIn('CASE-DEMO-1', {item['id'] for item in approvals.json()['items']})

        with self.db:
            self.assertEqual(self.db.execute('SELECT status FROM cases WHERE id=?', ('CASE-DEMO-1',)).fetchone()[0], 'AWAITING_SUPERVISOR')
            self.assertEqual(tuple(self.db.execute('SELECT from_role,to_role,status FROM pa_escalations WHERE case_id=?', ('CASE-DEMO-1',)).fetchone()), ('SPECIALIST', 'SUPERVISOR', 'ROUTED'))
            self.assertEqual(self.db.execute('SELECT supervisor_id FROM pa_case_assignments WHERE case_id=?', ('CASE-DEMO-1',)).fetchone()[0], 6)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pa_assignment_history WHERE case_id=?", ('CASE-DEMO-1',)).fetchone()[0], 2)
            audit_actions = {row[0] for row in self.db.execute('SELECT action FROM audit_events WHERE case_id=?', ('CASE-DEMO-1',))}
            self.assertIn('CASE_ESCALATED', audit_actions)
            self.assertIn('CASE_REASSIGNED', audit_actions)
            review = self.db.execute('SELECT old_status,new_status FROM pa_reviews WHERE case_id=?', ('CASE-DEMO-1',)).fetchone()
            self.assertEqual(tuple(review), ('NEW', 'AWAITING_SUPERVISOR'))

    def test_invalid_workflow_state_and_no_eligible_supervisor_are_rejected(self):
        self.db.execute("UPDATE cases SET status='AWAITING_MANAGER' WHERE id='CASE-DEMO-1'")
        self.db.commit()
        response = self.client.post(
            '/api/cases/CASE-DEMO-1/escalate', headers=self.headers('specialist'),
            json={'reason': 'Supervisor review required', 'explanation': 'Requires supervisor review.'},
        )
        self.assertEqual(response.status_code, 409)
        self.db.execute("UPDATE cases SET status='NEW' WHERE id='CASE-DEMO-1'")
        self.db.execute("UPDATE pa_staff SET active=0 WHERE role='SUPERVISOR'")
        self.db.commit()
        response = self.client.post(
            '/api/cases/CASE-DEMO-1/escalate', headers=self.headers('specialist'),
            json={'reason': 'Supervisor review required', 'explanation': 'Requires supervisor review.'},
        )
        self.assertEqual(response.status_code, 409)
        with self.db:
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pa_escalations').fetchone()[0], 0)
            self.assertEqual(self.db.execute('SELECT status FROM cases WHERE id=?', ('CASE-DEMO-1',)).fetchone()[0], 'NEW')


if __name__ == '__main__':
    unittest.main()
