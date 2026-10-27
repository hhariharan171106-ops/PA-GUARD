import hashlib
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import approval_api
import main
import security


class EvidenceVerificationSecurityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE pa_staff(id INTEGER PRIMARY KEY, username TEXT, role TEXT, active INTEGER);
            CREATE TABLE pa_sessions(token_hash TEXT PRIMARY KEY, staff_id INTEGER, expires_at TEXT);
            CREATE TABLE cases(id TEXT PRIMARY KEY);
            CREATE TABLE pa_case_assignments(case_id TEXT, specialist_id INTEGER, supervisor_id INTEGER);
            CREATE TABLE documents(id INTEGER PRIMARY KEY, case_id TEXT, name TEXT, status TEXT, reviewed_by TEXT, reviewed_at TEXT);
            CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, timestamp TEXT, actor TEXT, action TEXT, detail TEXT, decision_type TEXT);
        ''')
        users = [(1, 'spec', 'SPECIALIST'), (2, 'supervisor', 'SUPERVISOR'), (3, 'manager', 'MANAGER'), (4, 'admin', 'ADMIN')]
        for staff_id, username, role in users:
            self.db.execute('INSERT INTO pa_staff VALUES(?,?,?,1)', (staff_id, username, role))
            token = f'evidence-test-{username}'
            self.db.execute(
                'INSERT INTO pa_sessions VALUES(?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(), staff_id, '9999-12-31T23:59:59+00:00'),
            )
        self.db.executemany('INSERT INTO cases VALUES(?)', [('CASE-1',), ('CASE-2',)])
        self.db.execute('INSERT INTO pa_case_assignments VALUES(?,?,?)', ('CASE-1', 1, 2))
        self.db.executemany(
            'INSERT INTO documents(id,case_id,name,status) VALUES(?,?,?,?)',
            [(1, 'CASE-1', 'uploaded.pdf', 'UPLOADED'), (2, 'CASE-1', 'missing.pdf', 'MISSING'),
             (3, 'CASE-1', 'verified.pdf', 'VERIFIED'), (4, 'CASE-2', 'other-case.pdf', 'UPLOADED')],
        )
        self.db.commit()
        self.patches = [patch.object(module, 'connect', return_value=self.db) for module in (approval_api, main, security)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)

    def tearDown(self):
        self.db.close()

    def headers(self, username):
        return {'Authorization': f'Bearer evidence-test-{username}'}

    def test_manager_is_not_authorized_for_document_clinical_verification(self):
        roles = security.allowed_roles('POST', '/api/documents/1/verify')
        self.assertEqual(roles, {'SPECIALIST', 'SUPERVISOR'})
        response = self.client.post('/api/documents/1/verify', headers=self.headers('manager'), json={'actor': 'spoofed', 'note': 'Reviewed'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['detail'], 'Your role cannot access this API operation')
        with self.db:
            self.assertEqual(tuple(self.db.execute('SELECT status,reviewed_by FROM documents WHERE id=1').fetchone()), ('UPLOADED', None))
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0], 0)

    def test_supervisor_verification_persists_identity_timestamp_and_audit_once(self):
        payload = {'actor': 'spoofed actor', 'note': 'Reviewed the source document'}
        response = self.client.post('/api/documents/1/verify', headers=self.headers('supervisor'), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        with self.db:
            document = self.db.execute('SELECT status,reviewed_by,reviewed_at FROM documents WHERE id=1').fetchone()
            self.assertEqual(document['status'], 'VERIFIED')
            self.assertEqual(document['reviewed_by'], 'supervisor')
            self.assertTrue(document['reviewed_at'])
            datetime.fromisoformat(document['reviewed_at'])
            event = self.db.execute('SELECT actor,action,detail,decision_type FROM audit_events WHERE case_id=?', ('CASE-1',)).fetchone()
            self.assertEqual(tuple(event), ('supervisor', 'EVIDENCE_VERIFIED', 'Reviewed the source document', 'HUMAN_CLINICAL_REVIEW'))

        repeated = self.client.post('/api/documents/1/verify', headers=self.headers('supervisor'), json=payload)
        self.assertEqual(repeated.status_code, 409)
        with self.db:
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_events WHERE case_id=?', ('CASE-1',)).fetchone()[0], 1)

    def test_specialist_can_verify_but_case_scope_is_preserved(self):
        ok = self.client.post('/api/documents/1/verify', headers=self.headers('spec'), json={'actor': 'spoofed', 'note': 'Reviewed'})
        self.assertEqual(ok.status_code, 200, ok.text)
        other_case = self.client.post('/api/documents/4/verify', headers=self.headers('supervisor'), json={'actor': 'supervisor', 'note': 'Reviewed'})
        self.assertEqual(other_case.status_code, 404)

    def test_invalid_document_ids_states_and_payloads_are_rejected_without_audit(self):
        payload = {'actor': 'reviewer', 'note': 'Reviewed'}
        headers = self.headers('supervisor')
        self.assertEqual(self.client.post('/api/documents/999/verify', headers=headers, json=payload).status_code, 404)
        self.assertEqual(self.client.post('/api/documents/2/verify', headers=headers, json=payload).status_code, 409)
        self.assertEqual(self.client.post('/api/documents/3/verify', headers=headers, json=payload).status_code, 409)
        self.assertEqual(self.client.post('/api/documents/1/verify', headers=headers, json={'actor': 'reviewer'}).status_code, 422)
        self.assertEqual(self.client.post('/api/documents/1/verify', json=payload).status_code, 401)
        with self.db:
            self.assertEqual(self.db.execute('SELECT status FROM documents WHERE id=1').fetchone()[0], 'UPLOADED')
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit_events').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
