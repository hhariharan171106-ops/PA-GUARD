import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import approval_api
import database
import evidence_agents
import main
import security


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class EvidenceValidationAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        temp_root = Path(self.temp_dir.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DB_PATH", temp_root / "test_val.db")
        )
        self.stack.enter_context(
            patch.object(database, "DOCUMENT_STORAGE_DIR", temp_root / "documents")
        )

        def closing_connect():
            conn = sqlite3.connect(database.DB_PATH, factory=ClosingConnection)
            conn.row_factory = sqlite3.Row
            return conn

        for mod in (database, main, evidence_agents, approval_api, security):
            self.stack.enter_context(patch.object(mod, "connect", closing_connect))

        self.stack.enter_context(patch.object(main, "index_policies", lambda: {"indexed": 0}))
        self.stack.enter_context(
            patch.object(evidence_agents, "embed_text", lambda _text: ([1.0], "test-engine"))
        )
        self.stack.enter_context(
            patch.object(
                evidence_agents,
                "explain_evidence_with_ollama",
                lambda _facts: ("Human review is required for clinical necessity.", "test-engine"),
            )
        )

        database.initialize()
        self.client = self.stack.enter_context(TestClient(main.app))
        login = self.client.post("/api/auth/login", json={"username": "manager", "password": "ManagerDemo1"})
        self.assertEqual(login.status_code, 200, login.text)
        self.client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        self.addCleanup(self.stack.close)

    def _seed_test_case(self, case_id="CASE-SYN-1", payer="Northstar Health", service="Lumbar MRI"):
        with database.connect() as db:
            db.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    "PT-SYN-001",
                    payer,
                    service,
                    "2026-10-01",
                    "2026-10-15T12:00:00+00:00",
                    "EVIDENCE REVIEW",
                    "AT RISK",
                    "Test Coordinator",
                    "HIGH",
                ),
            )

    def test_document_classification_category_alone_does_not_satisfy_evidence_requirement(self):
        """A document category alone must NEVER establish that medical necessity is satisfied."""
        self._seed_test_case("CASE-CAT-TEST")
        with database.connect() as db:
            db.execute(
                """INSERT INTO documents(case_id,name,status,category,ocr_status,extracted_text)
                   VALUES(?,?,?,?,?,?)""",
                (
                    "CASE-CAT-TEST",
                    "lab_report_scan.pdf",
                    "REVIEW_REQUIRED",  # Not yet clinically verified by human
                    "Laboratory Reports",  # Classified category
                    "COMPLETED",
                    "Blood work panel results completed.",
                ),
            )

        result = evidence_agents.run_evidence_rag("CASE-CAT-TEST")
        validations = {v["requirement"]: v for v in result.get("evidence_validations", [])}
        
        # In Northstar lumbar MRI, requirements are Clinical notes and Conservative treatment history
        # Neither should be SATISFIED simply by having uploaded a document
        for req_name, validation in validations.items():
            self.assertNotEqual(
                validation["validation_status"],
                "SATISFIED",
                f"Requirement {req_name} should not be SATISFIED without human clinical verification",
            )
            self.assertTrue(validation["human_review_required"])

    def test_clinically_verified_document_satisfies_requirement(self):
        """Deterministic validation confirms SATISFIED only when human reviewer marks document VERIFIED."""
        self._seed_test_case("CASE-VERIFY-TEST")
        with database.connect() as db:
            db.execute(
                """INSERT INTO documents(case_id,name,status,category,ocr_status,extracted_text)
                   VALUES(?,?,?,?,?,?)""",
                (
                    "CASE-VERIFY-TEST",
                    "Clinical notes",
                    "VERIFIED",  # Verified by human reviewer
                    "Clinical Notes",
                    "COMPLETED",
                    "Clinical notes describe conservative treatment and physical examination.",
                ),
            )

        result = evidence_agents.run_evidence_rag("CASE-VERIFY-TEST")
        validations = {v["requirement"]: v for v in result.get("evidence_validations", [])}
        
        self.assertIn("Clinical notes", validations)
        self.assertEqual(validations["Clinical notes"]["validation_status"], "SATISFIED")
        self.assertFalse(validations["Clinical notes"]["human_review_required"])

    def test_missing_evidence_requirement_status(self):
        """Requirements with no supporting files or text in readable documents are marked MISSING."""
        self._seed_test_case("CASE-MISSING-TEST")
        with database.connect() as db:
            db.execute(
                """INSERT INTO documents(case_id,name,status,category,ocr_status,extracted_text)
                   VALUES(?,?,?,?,?,?)""",
                (
                    "CASE-MISSING-TEST",
                    "Clinical notes",
                    "REVIEW_REQUIRED",
                    "Clinical Notes",
                    "COMPLETED",
                    "Clinical notes describe patient symptoms only.",
                ),
            )

        result = evidence_agents.run_evidence_rag("CASE-MISSING-TEST")
        validations = {v["requirement"]: v for v in result.get("evidence_validations", [])}
        
        # Conservative treatment history is not present in document text or category
        self.assertIn("Conservative treatment history", validations)
        self.assertEqual(validations["Conservative treatment history"]["validation_status"], "MISSING")

    def test_uncertain_evidence_on_unreadable_or_failed_ocr(self):
        """Unreadable or OCR-failed documents preserve UNCERTAIN; OCR failure is not proof of absence."""
        self._seed_test_case("CASE-UNREADABLE-TEST")
        with database.connect() as db:
            db.execute(
                """INSERT INTO documents(case_id,name,status,category,ocr_status,extracted_text,processing_error)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    "CASE-UNREADABLE-TEST",
                    "corrupt_scan.pdf",
                    "REVIEW_REQUIRED",
                    "Clinical Notes",
                    "FAILED",
                    "",
                    "OCR failed without producing readable text.",
                ),
            )

        result = evidence_agents.run_evidence_rag("CASE-UNREADABLE-TEST")
        validations = {v["requirement"]: v for v in result.get("evidence_validations", [])}
        for req_name, validation in validations.items():
            self.assertEqual(validation["validation_status"], "UNCERTAIN")
            self.assertTrue(validation["human_review_required"])

    def test_policy_matching_by_payer_and_service(self):
        """Policy retrieval correctly filters by payer and service type."""
        self._seed_test_case("CASE-HARBOR-INF", payer="Harbor Mutual", service="Specialty infusion")
        result = evidence_agents.run_evidence_rag("CASE-HARBOR-INF")
        
        self.assertIn("Infusion order", result["required_documents"])
        self.assertIn("Lab results", result["required_documents"])
        self.assertEqual(result["policy_mode"], "DEMO_ONLY")
        self.assertTrue(all(ref["policy_use"] == "DEMO_ONLY" for ref in result["source_references"]))

    def test_unverified_public_policy_is_unavailable_not_authoritative(self):
        """A public label without source-level support cannot drive evidence validation."""
        self._seed_test_case("CASE-PUBLIC-UNVERIFIED", payer="CMS Medicare", service="Lumbar MRI")
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,version,approved,
                    payer,service_type,verification_status,required_documents,specific_evidence_requirements)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "POL-PUBLIC-UNVERIFIED", "PAYER_REQUIREMENT", "Unsubstantiated public label",
                    "Draft checklist.", "2018-04-10", "1.0", 1, "CMS Medicare", "Lumbar MRI",
                    "VERIFIED_PUBLIC", '["Clinical documentation of neurological deficit"]',
                    '["Clinical documentation of neurological deficit"]',
                ),
            )
        result = evidence_agents.run_evidence_rag("CASE-PUBLIC-UNVERIFIED")
        self.assertEqual(result["policy_mode"], "UNAVAILABLE")
        self.assertEqual(result["verification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["required_documents"], [])
        self.assertEqual(result["evidence_validations"], [])
        self.assertIn("metadata is incomplete", result["explanation"].lower())

    def test_unapproved_policy_is_not_active_and_no_policy_is_explicit(self):
        """Unapproved policy content is excluded; empty policy coverage stays unavailable."""
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,version,approved,
                    payer,service_type,verification_status,required_documents)
                   VALUES(?,?,?,?,?,?,0,?,?,?,?)""",
                (
                    "POL-UNAPPROVED", "PAYER_REQUIREMENT", "Unapproved draft", "Do not use.",
                    "2020-01-01", "1.0", "No Coverage Plan", "Lumbar MRI", "MOCK_DEMO",
                    '["Draft evidence"]',
                ),
            )
        self._seed_test_case("CASE-UNAPPROVED", payer="No Coverage Plan", service="Lumbar MRI")
        result = evidence_agents.run_evidence_rag("CASE-UNAPPROVED")
        self.assertEqual(result["policy_mode"], "UNAVAILABLE")
        self.assertEqual(result["required_documents"], [])
        self.assertEqual(result["verification_status"], "NEEDS_REVIEW")
        self.assertIn("no approved payer requirements", result["explanation"].lower())

    def test_no_policy_information_returns_explicit_unavailable_state(self):
        self._seed_test_case("CASE-NO-POLICY", payer="No Demo Data Plan", service="Unlisted Service")
        result = evidence_agents.run_evidence_rag("CASE-NO-POLICY")
        self.assertEqual(result["policy_mode"], "UNAVAILABLE")
        self.assertEqual(result["verification_status"], "NEEDS_REVIEW")
        self.assertEqual(result["required_documents"], [])
        self.assertEqual(result["evidence_validations"], [])
        self.assertTrue(result["needs_human_review"])

    def test_policy_for_wrong_service_is_not_selected(self):
        """A current approved policy for another service does not fill a service gap."""
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,version,approved,
                    payer,service_type,verification_status,required_documents)
                   VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    "POL-WRONG-SERVICE", "PAYER_REQUIREMENT", "Cervical MRI checklist", "Demo only.",
                    "2020-01-01", "1.0", "Service Specific Plan", "Cervical MRI", "MOCK_DEMO",
                    '["Cervical imaging report"]',
                ),
            )
        self._seed_test_case("CASE-WRONG-SERVICE", payer="Service Specific Plan", service="Lumbar MRI")
        result = evidence_agents.run_evidence_rag("CASE-WRONG-SERVICE")
        self.assertEqual(result["policy_mode"], "UNAVAILABLE")
        self.assertEqual(result["required_documents"], [])
        self.assertEqual(result["verification_status"], "NEEDS_REVIEW")

    def test_expired_policy_handling(self):
        """Policies with expires_at in the past are rejected with descriptive reason."""
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,expires_at,version,approved,payer,service_type,source_reference,required_documents)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    "POL-EXPIRED",
                    "PAYER_REQUIREMENT",
                    "Expired Cardiac Guideline",
                    "Expired requirements.",
                    "2020-01-01",
                    "2022-01-01",
                    "1.0",
                    "Expired Payer",
                    "Old Service",
                    "Expired Policy Doc",
                    '["Old Evidence"]',
                ),
            )
        self._seed_test_case("CASE-EXPIRED", payer="Expired Payer", service="Old Service")
        result = evidence_agents.run_evidence_rag("CASE-EXPIRED")
        
        self.assertEqual(result["verification_status"], "NEEDS_REVIEW")
        self.assertIn("expired as of review date", result["explanation"].lower())

    def test_future_not_yet_effective_policy_handling(self):
        """Policies with effective_date in the future are rejected with descriptive reason."""
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,expires_at,version,approved,payer,service_type,source_reference,required_documents)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    "POL-FUTURE",
                    "PAYER_REQUIREMENT",
                    "Future Cardiac Guideline",
                    "Future requirements.",
                    "2099-01-01",
                    None,
                    "1.0",
                    "Future Payer",
                    "Future Service",
                    "Future Policy Doc",
                    '["Future Evidence"]',
                ),
            )
        self._seed_test_case("CASE-FUTURE", payer="Future Payer", service="Future Service")
        result = evidence_agents.run_evidence_rag("CASE-FUTURE")
        
        self.assertEqual(result["verification_status"], "NEEDS_REVIEW")
        self.assertIn("not yet effective", result["explanation"].lower())

    def test_conflicting_policy_versions_handled_safely(self):
        """Multiple conflicting approved versions of the same policy trigger CONFLICTING validation."""
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,expires_at,version,approved,payer,service_type,source_reference,required_documents)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    "POL-CONFLICT",
                    "PAYER_REQUIREMENT",
                    "Conflicting Guideline v1",
                    "Version 1 requirements.",
                    "2026-01-01",
                    None,
                    "1.0",
                    "Conflict Payer",
                    "Conflict Service",
                    "Conflict Manual §1",
                    '["Requirement A"]',
                ),
            )
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,expires_at,version,approved,payer,service_type,source_reference,required_documents)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    "POL-CONFLICT",
                    "PAYER_REQUIREMENT",
                    "Conflicting Guideline v2",
                    "Version 2 requirements.",
                    "2026-01-01",
                    None,
                    "2.0",
                    "Conflict Payer",
                    "Conflict Service",
                    "Conflict Manual §2",
                    '["Requirement B"]',
                ),
            )
        self._seed_test_case("CASE-CONFLICT", payer="Conflict Payer", service="Conflict Service")
        result = evidence_agents.run_evidence_rag("CASE-CONFLICT")
        
        self.assertEqual(result["verification_status"], "NEEDS_REVIEW")
        self.assertIn("conflicting", result["explanation"].lower())

    def test_structured_policy_repository_fields_and_verification_status(self):
        """Unsubstantiated sample data stays unapproved and marked as demo."""
        response = self.client.get("/api/payer-requirements")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertGreater(len(items), 0)
        
        # Check that verification_status is present
        statuses = {item.get("verification_status") for item in items}
        self.assertTrue(statuses.issubset({"MOCK_DEMO", "VERIFIED_PUBLIC"}))
        
        # Verify CMS public policy exists
        cms_policies = [item for item in items if item.get("payer") == "CMS Medicare"]
        if cms_policies:
            self.assertEqual(cms_policies[0]["verification_status"], "MOCK_DEMO")
            self.assertEqual(cms_policies[0]["approved"], 0)
            self.assertIn("cms.gov", cms_policies[0]["source_reference"])

    def test_public_verification_requires_authoritative_source_review(self):
        payload = {
            "source_url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?NCDId=177",
            "authoritative_title": "Magnetic Resonance Imaging",
            "authoritative_version": "CMS NCD 220.2 effective 2018-04-10",
            "source_effective_date": "2018-04-10",
            "verification_note": "Reviewed the cited CMS NCD scope and confirmed each stored evidence requirement against the source.",
            "requirements_checked_against_source": True,
            "requirement_evidence": [
                {
                    "requirement": "Clinical documentation of neurological deficit",
                    "source_location": "NCD 220.2",
                    "supporting_excerpt": "No supporting language found for this draft requirement.",
                },
                {
                    "requirement": "Prior conservative therapy records",
                    "source_location": "NCD 220.2",
                    "supporting_excerpt": "No supporting language found for this draft requirement.",
                },
            ],
        }
        untrusted_source = self.client.post(
            "/api/payer-requirements/EVD-CMS-MRI/1.0/verify-source",
            json={**payload, "source_url": "https://cms-policy.example.org/ncd/177"},
        )
        self.assertEqual(untrusted_source.status_code, 422, untrusted_source.text)
        refused = self.client.post(
            "/api/payer-requirements/EVD-CMS-MRI/1.0/verify-source", json=payload
        )
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("does not substantiate", refused.json()["detail"])
        with database.connect() as db:
            row = db.execute(
                "SELECT verification_status,approved,source_verified_at FROM policy_documents WHERE policy_id='EVD-CMS-MRI' AND version='1.0'"
            ).fetchone()
        self.assertEqual(row["verification_status"], "MOCK_DEMO")
        self.assertEqual(row["approved"], 0)
        self.assertIsNone(row["source_verified_at"])

    def test_source_url_alone_cannot_publicly_verify_a_policy(self):
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents
                   (policy_id,document_type,title,content,effective_date,version,approved,
                    payer,service_type,source_reference,verification_status,required_documents,
                    specific_evidence_requirements)
                   VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                (
                    "CMS-URL-ONLY", "PAYER_REQUIREMENT", "CMS draft", "Draft only.",
                    "2018-04-10", "1.0", "CMS Medicare", "Other imaging", "https://www.cms.gov/",
                    "MOCK_DEMO", '["Document A", "Document B"]', '["Document A", "Document B"]',
                ),
            )
        payload = {
            "source_url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?NCDId=177",
            "authoritative_title": "Magnetic Resonance Imaging",
            "authoritative_version": "NCD 220.2",
            "source_effective_date": "2018-04-10",
            "verification_note": "Manager reviewed the authoritative source and policy scope.",
            "requirements_checked_against_source": True,
            "requirement_evidence": [
                {"requirement": "Document A", "source_location": "NCD 220.2", "supporting_excerpt": "A purported supporting quote long enough to pass validation."},
            ],
        }
        response = self.client.post(
            "/api/payer-requirements/CMS-URL-ONLY/1.0/verify-source", json=payload
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("for every stored evidence requirement", response.json()["detail"])
        with database.connect() as db:
            row = db.execute(
                "SELECT verification_status,source_verified_at,source_requirement_evidence FROM policy_documents WHERE policy_id='CMS-URL-ONLY'"
            ).fetchone()
        self.assertEqual(row["verification_status"], "MOCK_DEMO")
        self.assertIsNone(row["source_verified_at"])
        self.assertIsNone(row["source_requirement_evidence"])

    def test_mocked_source_match_requires_manager_approval_and_evidence_review_is_audited(self):
        self._seed_test_case("CASE-SOURCE-TRAIL")
        source_url = "https://www.cms.gov/synthetic-test-source"
        quote = "Synthetic source passage used only to exercise the isolated verification workflow."
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents(policy_id,document_type,title,content,effective_date,version,approved,
                   payer,service_type,source_reference,verification_status,required_documents,specific_evidence_requirements)
                   VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                ("POLICY-TEST-ONLY", "PAYER_REQUIREMENT", "Synthetic test policy", "Test only", "2026-01-01", "1",
                 "Northstar Health", "Lumbar MRI", source_url, "MOCK_DEMO", '["Synthetic imaging note"]', '["Synthetic imaging note"]'),
            )
        payload = {
            "source_url": source_url,
            "authoritative_title": "Synthetic fixture source",
            "authoritative_version": "fixture-v1",
            "verification_note": "Synthetic isolated test fixture; not a real policy verification.",
            "requirements_checked_against_source": True,
            "requirement_evidence": [{"requirement": "Synthetic imaging note", "source_location": "fixture paragraph 1", "supporting_excerpt": quote}],
        }
        with patch.object(main, "_fetch_policy_source", return_value=(quote, "a" * 64)):
            verified = self.client.post("/api/payer-requirements/POLICY-TEST-ONLY/1/verify-source", json=payload)
        self.assertEqual(verified.status_code, 202, verified.text)
        self.assertEqual(verified.json()["verification_status"], "MOCK_DEMO")
        criterion_id = "CR-" + __import__("hashlib").sha256(b"POLICY-TEST-ONLY|1|Synthetic imaging note").hexdigest()[:20]
        with database.connect() as db:
            self.assertEqual(db.execute("SELECT approved FROM policy_documents WHERE policy_id='POLICY-TEST-ONLY'").fetchone()[0], 0)
        approved = self.client.post("/api/payer-requirements/POLICY-TEST-ONLY/1/approve-source", json={"reason": "Manager reviewed the isolated synthetic source fixture."})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["verification_status"], "VERIFIED_PUBLIC")
        with database.connect() as db:
            db.execute("INSERT INTO documents(case_id,name,status,category,ocr_status,extracted_text) VALUES(?,?,?,?,?,?)",
                       ("CASE-SOURCE-TRAIL", "Synthetic note", "RECEIVED", "CLINICAL_NOTES", "COMPLETED", "Synthetic document passage for human review."))
            document_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        trail = self.client.get("/api/cases/CASE-SOURCE-TRAIL/evidence-trail")
        self.assertEqual(trail.status_code, 200, trail.text)
        self.assertTrue(trail.json()["available"])
        review = self.client.post(f"/api/cases/CASE-SOURCE-TRAIL/evidence-trail/{criterion_id}/review", json={
            "document_id": document_id, "assessment_status": "MATCH_POSSIBLE", "review_status": "HUMAN_CONFIRMED",
            "supporting_excerpt": "Synthetic document passage for human review.", "source_locator": "page 1",
            "review_notes": "Synthetic test review only.",
        })
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["reviewer_id"], "manager")
        self.assertIsNotNone(review.json()["audit_event_id"])

    def test_source_verification_fails_closed_for_unauthorized_missing_and_unavailable_source(self):
        with database.connect() as db:
            db.execute(
                """INSERT INTO policy_documents(policy_id,document_type,title,content,effective_date,version,approved,
                   payer,service_type,source_reference,verification_status,required_documents,specific_evidence_requirements)
                   VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                ("POLICY-SOURCE-ERROR", "PAYER_REQUIREMENT", "Synthetic test policy", "Test only", "2026-01-01", "1",
                 "Northstar Health", "Lumbar MRI", "https://www.cms.gov/synthetic-test-source", "MOCK_DEMO", '["Synthetic criterion"]', '["Synthetic criterion"]'),
            )
        payload = {
            "source_url": "https://www.cms.gov/synthetic-test-source",
            "authoritative_title": "Synthetic fixture source", "authoritative_version": "fixture-v1",
            "verification_note": "Testing an unavailable source without changing policy verification.",
            "requirements_checked_against_source": True,
            "requirement_evidence": [{"requirement": "Synthetic criterion", "source_location": "fixture paragraph 1", "supporting_excerpt": "Synthetic exact source excerpt for the isolated unavailable-source test."}],
        }
        with patch.object(main, "_fetch_policy_source", side_effect=RuntimeError("Synthetic source outage")):
            unavailable = self.client.post("/api/payer-requirements/POLICY-SOURCE-ERROR/1/verify-source", json=payload)
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        with database.connect() as db:
            attempt = db.execute("SELECT attempt_status FROM policy_source_verification_attempts WHERE policy_id='POLICY-SOURCE-ERROR'").fetchone()
            policy = db.execute("SELECT verification_status,approved FROM policy_documents WHERE policy_id='POLICY-SOURCE-ERROR'").fetchone()
        self.assertEqual(attempt["attempt_status"], "SOURCE_UNAVAILABLE")
        self.assertEqual((policy["verification_status"], policy["approved"]), ("MOCK_DEMO", 0))
        missing = self.client.post("/api/payer-requirements/NO-SUCH-POLICY/1/verify-source", json=payload)
        self.assertEqual(missing.status_code, 404, missing.text)
        specialist = self.client.post("/api/auth/login", json={"username": "specialist", "password": "SpecialistDemo1"})
        self.assertEqual(specialist.status_code, 200, specialist.text)
        denied = self.client.post("/api/payer-requirements/POLICY-SOURCE-ERROR/1/verify-source", json=payload,
                                  headers={"Authorization": f"Bearer {specialist.json()['token']}"})
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_database_migration_downgrades_unsubstantiated_cms_row_idempotently(self):
        with database.connect() as db:
            db.execute(
                """UPDATE policy_documents SET verification_status='VERIFIED_PUBLIC',approved=1,
                   effective_date='2026-01-01',source_url='https://www.cms.gov/',
                   authoritative_title='Magnetic Resonance Imaging',authoritative_version='NCD 220.2',
                   source_verified_by='Prior reviewer',source_verified_at='2026-01-01T00:00:00Z',
                   source_verification_note='URL checked',source_requirement_evidence='[]'
                   WHERE policy_id='EVD-CMS-MRI' AND version='1.0'"""
            )
        database.initialize()
        database.initialize()
        with database.connect() as db:
            row = db.execute(
                "SELECT * FROM policy_documents WHERE policy_id='EVD-CMS-MRI' AND version='1.0'"
            ).fetchone()
            event_count = db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action='PAYER_POLICY_VERIFICATION_DOWNGRADED'"
            ).fetchone()[0]
        self.assertEqual(row["verification_status"], "MOCK_DEMO")
        self.assertEqual(row["approved"], 0)
        self.assertEqual(row["effective_date"], "2018-04-10")
        self.assertIsNone(row["source_verified_at"])
        self.assertIsNone(row["source_requirement_evidence"])
        self.assertEqual(event_count, 1)

    def test_requirement_create_cannot_self_assign_public_verification(self):
        payload = {
            "requirement_id": "UNVERIFIED-PUBLIC",
            "verification_status": "VERIFIED_PUBLIC",
            "document_type": "PAYER_REQUIREMENT",
            "title": "Unverified payer requirement",
            "payer": "CMS Medicare",
            "service_type": "Lumbar MRI",
            "required_documents": ["Clinical notes"],
            "content": "Draft only.",
            "source_reference": "https://example.org/policy",
            "effective_date": "2026-01-01",
            "version": "1.0",
        }
        response = self.client.post("/api/payer-requirements", json=payload)
        self.assertEqual(response.status_code, 422, response.text)

    def test_evidence_prediction_api_compatibility_and_validation_status(self):
        """Evidence prediction endpoint returns both ml_prediction and separate validation_status."""
        self._seed_test_case("CASE-PREDICT-TEST")
        with database.connect() as db:
            db.execute(
                """INSERT INTO documents(case_id,name,status,category,ocr_status,extracted_text)
                   VALUES(?,?,?,?,?,?)""",
                (
                    "CASE-PREDICT-TEST",
                    "Clinical notes",
                    "REVIEW_REQUIRED",
                    "Clinical Notes",
                    "COMPLETED",
                    "Clinical notes describe symptoms, examination, and the treatment plan.",
                ),
            )

        response = self.client.post("/api/agents/evidence/cases/CASE-PREDICT-TEST/predict", json={})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        
        self.assertIn("finding", data)
        self.assertIn("prediction", data)
        prediction = data["prediction"]
        self.assertTrue(prediction["available"])
        
        for item in prediction["predictions"]:
            self.assertIn("required_document", item)
            self.assertIn("ml_prediction", item)
            self.assertIn("verification_status", item)
            self.assertIn("validation_status", item)
            self.assertIn(item["validation_status"], {"SATISFIED", "MISSING", "UNCERTAIN", "CONFLICTING"})
            # Ensure ml_prediction does not equal or dictate validation_status
            if item["ml_prediction"] == "NOT_PREDICTED_MISSING":
                # Without human review, validation status remains UNCERTAIN
                self.assertNotEqual(item["validation_status"], "SATISFIED")


if __name__ == "__main__":
    unittest.main()
