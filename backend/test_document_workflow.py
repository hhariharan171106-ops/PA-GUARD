import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pdf2image.exceptions import PDFInfoNotInstalledError
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from pytesseract.pytesseract import TesseractNotFoundError

import database
import approval_api
import agents
import case_workflow
import escalation_agents
import evidence_agents
import main
import rag
import recovery_agents
import security
import sla_risk_service


TESSERACT_AVAILABLE = bool(
    shutil.which("tesseract")
    or Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").is_file()
    or Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe").is_file()
)
POPPLER_AVAILABLE = bool(
    shutil.which("pdftoppm")
    or (
        os.environ.get("POPPLER_PATH")
        and (Path(os.environ["POPPLER_PATH"]) / "pdftoppm.exe").is_file()
    )
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def text_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})
    })
    stream = DecodedStreamObject()
    escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped_text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def scanned_pdf() -> bytes:
    output = BytesIO()
    with Image.open(BytesIO(scanned_png())) as image:
        image.convert("RGB").save(output, format="PDF", resolution=150)
    return output.getvalue()


def scanned_png() -> bytes:
    image = Image.new("RGB", (1500, 260), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 64)
    except OSError:
        font = ImageFont.load_default()
    draw.text((30, 70), "Clinical notes readable synthetic document", fill="black", font=font)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def scanned_jpeg() -> bytes:
    output = BytesIO()
    with Image.open(BytesIO(scanned_png())) as image:
        image.save(output, format="JPEG", quality=95)
    return output.getvalue()


class DocumentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        temporary_root = Path(self.temp_directory.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(database, "DB_PATH", temporary_root / "test.db"))
        storage_root = temporary_root / "documents"
        self.stack.enter_context(patch.object(database, "DOCUMENT_STORAGE_DIR", storage_root))
        def closing_connect():
            connection = sqlite3.connect(database.DB_PATH, factory=ClosingConnection)
            connection.row_factory = sqlite3.Row
            return connection

        self.stack.enter_context(patch.object(database, "connect", closing_connect))
        self.stack.enter_context(patch.object(main, "connect", closing_connect))
        self.stack.enter_context(patch.object(approval_api, "connect", closing_connect))
        self.stack.enter_context(patch.object(security, "connect", closing_connect))
        self.stack.enter_context(patch.object(agents, "connect", closing_connect))
        self.stack.enter_context(patch.object(rag, "connect", closing_connect))
        self.stack.enter_context(patch.object(escalation_agents, "connect", closing_connect))
        self.stack.enter_context(patch.object(recovery_agents, "connect", closing_connect))
        self.stack.enter_context(patch.object(sla_risk_service, "connect", closing_connect))
        self.stack.enter_context(patch.object(case_workflow, "connect", closing_connect))
        self.stack.enter_context(patch.object(evidence_agents, "connect", closing_connect))
        self.stack.enter_context(patch.object(main, "SYSTEM_DOCUMENT_ROOT", storage_root))
        self.stack.enter_context(patch.object(main, "index_policies", lambda: {"indexed": 0}))
        self.stack.enter_context(patch.object(evidence_agents, "embed_text", lambda _text: ([1.0], "test-local")))
        self.stack.enter_context(
            patch.object(
                evidence_agents,
                "explain_evidence_with_ollama",
                lambda _facts: (
                    "Extracted text was compared with approved requirement labels; human review is required.",
                    "test-local",
                ),
            )
        )
        self.client = self.stack.enter_context(TestClient(main.app))
        login = self.client.post("/api/auth/login", json={"username":"manager","password":"ManagerDemo1"})
        self.assertEqual(login.status_code, 200, login.text)
        self.client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
        self.addCleanup(self.stack.close)
        self.storage_root = storage_root

    def add_northstar_case(self, case_id: str) -> None:
        with database.connect() as db:
            db.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    "SYNTHETIC-001",
                    "Northstar Health",
                    "Lumbar MRI",
                    "2026-10-10",
                    "2026-10-12T10:00:00+00:00",
                    "INTAKE REVIEW",
                    "ON TRACK",
                    "Test Coordinator",
                    "MEDIUM",
                ),
            )

    def add_northstar_cardiac_ct_case(self, case_id: str) -> None:
        with database.connect() as db:
            db.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    "SYNTHETIC-CT-001",
                    "Northstar Health",
                    "Cardiac CT",
                    "2026-10-10",
                    "2026-10-12T10:00:00+00:00",
                    "INTAKE REVIEW",
                    "ON TRACK",
                    "Test Coordinator",
                    "MEDIUM",
                ),
            )

    def upload(
        self,
        filename: str,
        content: bytes,
        category: str = "Clinical Notes",
        case_id: str = "PA-1042",
        submission_id: str | None = None,
    ):
        return self.client.post(
            f"/api/cases/{case_id}/documents",
            files={"file": (filename, content, "application/octet-stream")},
            data={
                "category": category,
                **({"submission_id": submission_id} if submission_id else {}),
            },
        )

    def upload_batch(self, case_id: str, documents: list[tuple[str, str, bytes, str]]):
        return self.client.post(
            f"/api/cases/{case_id}/documents/batch",
            files=[
                ("files", (filename, content, "application/pdf"))
                for filename, _category, content, _submission_id in documents
            ],
            data={
                "categories": [category for _filename, category, _content, _submission_id in documents],
                "submission_ids": [submission_id for _filename, _category, _content, submission_id in documents],
            },
        )

    def test_text_pdf_upload_extracts_text_and_persists_metadata_safely(self) -> None:
        content = text_pdf("Clinical notes and conservative treatment history are documented.")
        response = self.upload(r"..\..\synthetic-clinical-notes.pdf", content)

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["case_id"], "PA-1042")
        self.assertEqual(document["original_filename"], "synthetic-clinical-notes.pdf")
        self.assertEqual(document["ocr_status"], "COMPLETED")
        self.assertIn("Clinical notes", document["extracted_text"])
        self.assertNotIn("file_path", document)
        with database.connect() as db:
            stored = dict(db.execute("SELECT * FROM documents WHERE id=?", (document["id"],)).fetchone())
        self.assertEqual(stored["case_id"], "PA-1042")
        self.assertEqual(stored["original_filename"], "synthetic-clinical-notes.pdf")
        self.assertEqual(stored["ocr_status"], "COMPLETED")
        self.assertEqual(stored["file_size"], len(content))
        self.assertEqual(Path(stored["file_path"]).parent.parent, self.storage_root)
        self.assertTrue(Path(stored["file_path"]).is_file())
        self.assertEqual(json.loads(stored["page_text"])[0].strip(), document["extracted_text"].strip())
        retrieved = self.client.get(f"/api/documents/{document['id']}")
        text_response = self.client.get(f"/api/documents/{document['id']}/text")
        self.assertEqual(retrieved.status_code, 200, retrieved.text)
        self.assertEqual(retrieved.json()["extracted_text"], document["extracted_text"])
        self.assertEqual(text_response.json()["text"], document["extracted_text"])

    def test_five_documents_upload_and_persist_to_one_case(self) -> None:
        case_id = "PA-CT-FIVE"
        self.add_northstar_cardiac_ct_case(case_id)
        sample_documents = [
            ("clinical-report.pdf", "Clinical Reports", "Fictional patient CT symptoms and clinical assessment."),
            ("imaging-report.pdf", "Imaging Reports", "Fictional cardiac CT imaging report and scan impression."),
            ("treatment-report.pdf", "Treatment Reports", "Fictional prior treatment history and response summary."),
            ("medical-necessity.pdf", "Medical Necessity Documents", "Fictional medical necessity rationale for requested imaging."),
            ("prescription.pdf", "Prescription", "Fictional prescription record and requested service details."),
        ]
        response = self.upload_batch(
            case_id,
            [
                (filename, category, text_pdf(text), f"five-doc-{index}")
                for index, (filename, category, text) in enumerate(sample_documents, start=1)
            ],
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["case_id"], case_id)
        self.assertTrue(result["all_succeeded"])
        self.assertFalse(result["partial_success"])
        self.assertEqual(result["failures"], [])
        uploaded = result["documents"]
        self.assertEqual(len({document["id"] for document in uploaded}), 5)
        self.assertTrue(all(document["case_id"] == case_id for document in uploaded))
        self.assertTrue(all(document["ocr_status"] == "COMPLETED" for document in uploaded))
        self.assertTrue(all(document["extracted_text"] for document in uploaded))
        with database.connect() as db:
            saved = db.execute(
                "SELECT COUNT(*) AS count FROM documents WHERE case_id=? AND submission_id LIKE 'five-doc-%'",
                (case_id,),
            ).fetchone()["count"]
        self.assertEqual(saved, 5)

    def test_specialist_can_upload_two_documents_to_an_assigned_case(self) -> None:
        with database.connect() as db:
            specialist = db.execute(
                "SELECT id FROM pa_staff WHERE username='specialist'"
            ).fetchone()
            self.assertIsNotNone(specialist)
            db.execute(
                "INSERT INTO pa_case_assignments(case_id,specialist_id,updated_at) VALUES(?,?,?)",
                ("PA-1042", specialist["id"], "2026-10-23T00:00:00+00:00"),
            )
        login = self.client.post(
            "/api/auth/login",
            json={"username": "specialist", "password": "SpecialistDemo1"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['token']}"}

        response = self.client.post(
            "/api/cases/PA-1042/documents/batch",
            headers=headers,
            files=[
                ("files", ("synthetic-clinical.pdf", text_pdf("Synthetic clinical notes for a fictional case."), "application/pdf")),
                ("files", ("synthetic-treatment.pdf", text_pdf("Synthetic treatment history for a fictional case."), "application/pdf")),
            ],
            data={
                "categories": ["Clinical Reports", "Treatment Reports"],
                "submission_ids": ["specialist-batch-1", "specialist-batch-2"],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["case_id"], "PA-1042")
        self.assertTrue(result["all_succeeded"])
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["saved_submission_ids"], ["specialist-batch-1", "specialist-batch-2"])
        self.assertEqual(len(result["documents"]), 2)
        self.assertTrue(all(document["extracted_text"] for document in result["documents"]))

    def test_unassigned_specialist_cannot_upload_to_a_case(self) -> None:
        case_id = "PA-TEST-UPLOAD-UNASSIGNED"
        self.add_northstar_case(case_id)
        login = self.client.post(
            "/api/auth/login",
            json={"username": "specialist", "password": "SpecialistDemo1"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        response = self.client.post(
            f"/api/cases/{case_id}/documents/batch",
            headers={"Authorization": f"Bearer {login.json()['token']}"},
            files=[("files", ("synthetic.pdf", text_pdf("Synthetic report for a fictional case."), "application/pdf"))],
            data={"categories": ["Clinical Reports"], "submission_ids": ["unassigned-upload-1"]},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"], "Case not found")
        with database.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM documents WHERE submission_id=?", ("unassigned-upload-1",)).fetchone()[0],
                0,
            )

    def test_batch_upload_reports_individual_invalid_file_failure(self) -> None:
        response = self.upload_batch(
            "PA-1042",
            [
                ("valid-report.pdf", "Clinical Reports", text_pdf("Fictional clinical report with sufficient text."), "batch-valid-1"),
                ("invalid-report.pdf", "Imaging Reports", b"not a pdf", "batch-invalid-1"),
            ],
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["partial_success"])
        self.assertFalse(result["all_succeeded"])
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["failures"][0]["filename"], "invalid-report.pdf")
        self.assertEqual(result["failures"][0]["submission_id"], "batch-invalid-1")
        self.assertIn("Invalid document content", result["failures"][0]["message"])
        original_saved_id = result["documents"][0]["id"]
        retry = self.upload_batch(
            "PA-1042",
            [
                ("valid-report.pdf", "Clinical Reports", text_pdf("Fictional clinical report with sufficient text."), "batch-valid-1"),
                ("invalid-report.pdf", "Imaging Reports", text_pdf("Fictional imaging report corrected for retry."), "batch-invalid-1"),
            ],
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["all_succeeded"])
        self.assertEqual(retry.json()["documents"][0]["id"], original_saved_id)
        self.assertEqual(retry.json()["saved_submission_ids"], ["batch-valid-1", "batch-invalid-1"])
        with database.connect() as db:
            saved = db.execute(
                "SELECT COUNT(*) AS count FROM documents WHERE case_id=? AND submission_id LIKE 'batch-%'",
                ("PA-1042",),
            ).fetchone()["count"]
        self.assertEqual(saved, 2)

    def test_batch_upload_rejects_empty_selection(self) -> None:
        response = self.client.post("/api/cases/PA-1042/documents/batch", data={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Select at least one document", response.json()["detail"])

    def test_batch_upload_rejects_mismatched_fields_and_duplicate_submission_ids(self) -> None:
        content = text_pdf("Synthetic clinical report for multipart validation.")
        mismatched = self.client.post(
            "/api/cases/PA-1042/documents/batch",
            files=[("files", ("report.pdf", content, "application/pdf"))],
            data={"categories": [], "submission_ids": ["multipart-1"]},
        )
        self.assertEqual(mismatched.status_code, 400, mismatched.text)

        duplicate = self.upload_batch(
            "PA-1042",
            [
                ("one.pdf", "Clinical Reports", content, "duplicate-in-batch"),
                ("two.pdf", "Imaging Reports", content, "duplicate-in-batch"),
            ],
        )
        self.assertEqual(duplicate.status_code, 400, duplicate.text)
        self.assertIn("must be unique", duplicate.json()["detail"])
        with database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM documents WHERE submission_id=?",
                    ("duplicate-in-batch",),
                ).fetchone()[0],
                0,
            )

    def test_batch_upload_rejects_missing_case_before_persisting_files(self) -> None:
        response = self.upload_batch(
            "PA-DOES-NOT-EXIST",
            [("missing-case.pdf", "Clinical Reports", text_pdf("Synthetic report text."), "missing-case-1")],
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"], "Case not found")

    def test_workflow_failures_endpoint_allows_manager_and_requires_authorization(self) -> None:
        with database.connect() as db:
            db.execute(
                "INSERT INTO workflow_failures(case_id,occurred_at,error_code,error_details,workflow_state) VALUES(?,?,?,?,?)",
                ("PA-1042", "2026-10-22T10:00:00+00:00", "TEST_FAILURE", "Synthetic test failure", "EVIDENCE"),
            )
        result = self.client.get("/api/workflow-failures")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(any(row["error_code"] == "TEST_FAILURE" for row in result.json()))

        unauthenticated = self.client.get(
            "/api/workflow-failures", headers={"Authorization": "Bearer invalid"}
        )
        self.assertEqual(unauthenticated.status_code, 401)

    def test_submission_id_makes_upload_retry_idempotent_without_running_agents(self) -> None:
        content = text_pdf("Clinical findings support cardiac CT imaging.")
        with patch.object(main, "run_evidence_rag") as evidence_agent:
            first = self.upload(
                "cardiology-report.pdf",
                content,
                category="Clinical Reports",
                submission_id="test-submission-001",
            )
            retry = self.upload(
                "cardiology-report.pdf",
                content,
                category="Clinical Reports",
                submission_id="test-submission-001",
            )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        evidence_agent.assert_not_called()
        with database.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS count FROM documents WHERE submission_id=?",
                ("test-submission-001",),
            ).fetchone()["count"]
        self.assertEqual(count, 1)

    def test_case_workflow_runs_existing_stages_and_explains_actual_results(self) -> None:
        with database.connect() as db:
            db.execute("DELETE FROM workflow_failures WHERE case_id=?", ("PA-1042",))
        evidence_result = {
            "case_id": "PA-1042",
            "required_documents": ["Cardiology report"],
            "found_documents": [],
            "possible_evidence_matches": [],
            "missing_documents": ["Cardiology report"],
            "unreadable_documents": [],
            "possible_mismatches": [],
            "verification_status": "NEEDS_REVIEW",
            "explanation": "Evidence requires review.",
            "recommended_action": "Review the clinical record.",
            "source_references": [],
            "needs_human_review": True,
        }
        escalation_result = {
            "case_id": "PA-1042",
            "escalation_required": False,
            "escalation_level": "NONE",
            "reason": "No escalation threshold was met.",
            "responsible_role": "",
            "recommended_action": "Continue routine case review.",
            "approval_required": False,
            "source_references": [],
            "notification_status": "NOT_REQUIRED",
        }
        risk_result = {
            "case_id": "PA-1042",
            "available": True,
            "risk_level": "MEDIUM",
            "risk_score": 40.0,
            "score_meaning": "Synthetic test score.",
            "factors": [],
            "reliable": False,
            "reliability_note": "Test fixture only.",
            "training_data_source": "synthetic test",
        }

        with (
            patch.object(case_workflow, "run_evidence_rag", return_value=evidence_result),
            patch.object(case_workflow, "predict_evidence_gaps", return_value={"available": False, "requirements_available": True, "experimental": True, "validated": False, "training_data_source": "synthetic test", "model_name": "test", "message": "Test prediction.", "predictions": []}),
            patch.object(case_workflow, "run_sla_rag", return_value={"recommended_action": "Review deadline."}),
            patch.object(case_workflow, "predict_case_sla_risk", return_value=risk_result),
            patch.object(case_workflow, "run_escalation_rag", return_value=escalation_result),
        ):
            response = self.client.post(
                "/api/agents/cases/PA-1042/workflow",
                json={"actor": "Test Reviewer"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["stages"]["evidence_agent"]["status"], "completed")
        self.assertEqual(result["stages"]["sla_risk_prediction"]["result"]["risk_score"], 40.0)
        self.assertEqual(result["stages"]["recovery_agent"]["status"], "skipped")
        self.assertEqual(result["llm_explanation"]["engine"], case_workflow.DETERMINISTIC_EXPLANATION_ENGINE)
        self.assertIn("Cardiology report: MISSING", result["llm_explanation"]["explanation"])

        summary = result["summary"]
        with database.connect() as db:
            actual_unverified = db.execute(
                "SELECT COUNT(*) FROM documents WHERE case_id=? AND status!='VERIFIED'",
                ("PA-1042",),
            ).fetchone()[0]
        self.assertEqual(summary["unverified_document_count"], actual_unverified)
        self.assertEqual(len(summary["unverified_documents"]), actual_unverified)

    def test_ocr_match_never_becomes_human_verification(self) -> None:
        finding = {
            "required_documents": ["Clinical notes"],
            "found_documents": [{"required_document": "Clinical notes", "document_name": "visit.pdf", "category": "Clinical Reports", "page_number": 2, "excerpt": "Patient reports symptoms."}],
            "possible_evidence_matches": [], "missing_documents": [], "unreadable_documents": [],
            "evidence_validations": [],
        }
        evidence = case_workflow._evidence_summary(finding, "completed")[0]
        self.assertEqual(evidence["status"], "NEEDS_REVIEW")
        self.assertTrue(evidence["human_review_required"])
        self.assertEqual(evidence["supporting_documents"][0]["page_number"], 2)
        self.assertIn("not clinical verification", evidence["explanation"])

    def test_responsible_staff_uses_assignments_and_escalation_acceptance_state(self) -> None:
        assignment = {"specialist_id": 1, "specialist_name": "Assigned Specialist", "specialist_role": "SPECIALIST", "specialist_active": 1}
        routed = {"id": 4, "status": "ROUTED", "to_role": "SUPERVISOR", "to_staff_name": "Receiving Supervisor", "to_staff_role": "SUPERVISOR"}
        responsible, escalation = case_workflow._resolve_responsible_staff(assignment, [routed])
        self.assertEqual(responsible["name"], "Assigned Specialist")
        self.assertEqual(escalation["status"], "ROUTED")
        self.assertEqual(escalation["assignee"]["name"], "Receiving Supervisor")

        accepted = {**routed, "status": "ACCEPTED"}
        responsible, escalation = case_workflow._resolve_responsible_staff(assignment, [accepted])
        self.assertEqual(responsible["name"], "Receiving Supervisor")
        self.assertEqual(responsible["source"], "accepted escalation")
        self.assertEqual(escalation["status"], "ACCEPTED")

    def test_inactive_or_missing_assignment_has_no_operational_assignee(self) -> None:
        inactive = {"specialist_id": 9, "specialist_name": "Inactive Staff", "specialist_role": "SPECIALIST", "specialist_active": 0}
        self.assertEqual(case_workflow._resolve_responsible_staff(inactive, []), (None, None))
        self.assertEqual(case_workflow._resolve_responsible_staff(None, []), (None, None))
        invalid_role = {"specialist_id": 9, "specialist_name": "Wrong Slot", "specialist_role": "MANAGER", "specialist_active": 1}
        self.assertEqual(case_workflow._resolve_responsible_staff(invalid_role, []), (None, None))

    @unittest.skipUnless(
        os.environ.get("PA_GUARD_LIVE_OLLAMA") == "1",
        "Set PA_GUARD_LIVE_OLLAMA=1 to run the local Ollama integration smoke test.",
    )
    def test_live_ollama_workflow_explanation_is_persisted_and_returned(self) -> None:
        ollama = main.ollama_status()
        if not ollama["model_available"]:
            self.skipTest(ollama["message"])
        self.add_northstar_cardiac_ct_case("PA-OLLAMA-LIVE")
        finding = {
            "case_id": "PA-OLLAMA-LIVE",
            "required_documents": [],
            "found_documents": [],
            "possible_evidence_matches": [],
            "missing_documents": [],
            "unreadable_documents": [],
            "possible_mismatches": [],
            "verification_status": "NEEDS_REVIEW",
            "explanation": "No approved requirement labels are configured for this synthetic case.",
            "recommended_action": "Have an authorized reviewer check the available information.",
            "source_references": [],
            "needs_human_review": True,
        }
        escalation = {
            "case_id": "PA-OLLAMA-LIVE",
            "escalation_required": False,
            "escalation_level": "NONE",
            "reason": "The synthetic case meets no configured escalation trigger.",
            "responsible_role": "",
            "recommended_action": "Continue routine human review.",
            "approval_required": False,
            "source_references": [],
            "notification_status": "NOT_REQUIRED",
        }
        with (
            patch.object(case_workflow, "run_evidence_rag", return_value=finding),
            patch.object(case_workflow, "run_sla_rag", return_value={"recommended_action": "Review the deadline."}),
            patch.object(case_workflow, "run_escalation_rag", return_value=escalation),
        ):
            response = self.client.post(
                "/api/agents/cases/PA-OLLAMA-LIVE/workflow",
                json={"actor": "Synthetic Live Ollama Test"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["llm_explanation"]["engine"], f"ollama:{ollama['model']}")
        explanation = result["llm_explanation"]["explanation"]
        self.assertGreater(len(explanation), 30)
        self.assertFalse(explanation.lstrip().startswith(("{", "[")))
        with database.connect() as db:
            stored = db.execute(
                "SELECT result_json FROM case_workflow_state WHERE case_id=?",
                ("PA-OLLAMA-LIVE",),
            ).fetchone()
        self.assertIsNotNone(stored)
        stored_result = json.loads(stored["result_json"])
        self.assertEqual(stored_result["llm_explanation"]["explanation"], explanation)
        self.assertEqual(stored_result["llm_explanation"]["engine"], f"ollama:{ollama['model']}")

    def test_workflow_retry_reruns_failed_stages_and_reuses_completed_results(self) -> None:
        evidence_result = {
            "case_id": "PA-1042",
            "required_documents": [],
            "found_documents": [],
            "possible_evidence_matches": [],
            "missing_documents": [],
            "unreadable_documents": [],
            "possible_mismatches": [],
            "verification_status": "NEEDS_REVIEW",
            "explanation": "Synthetic test result.",
            "recommended_action": "Human review.",
            "source_references": [],
            "needs_human_review": True,
        }
        escalation_result = {
            "case_id": "PA-1042",
            "escalation_required": False,
            "escalation_level": "NONE",
            "reason": "No escalation threshold was met.",
            "responsible_role": "",
            "recommended_action": "Continue routine case review.",
            "approval_required": False,
            "source_references": [],
            "notification_status": "NOT_REQUIRED",
        }
        recovery_result = {
            "case_id": "PA-1042",
            "failure_type": "CASE_WORKFLOW_EVIDENCE_AGENT",
            "error_details": "Synthetic failed stage.",
            "likely_cause": "Temporary test failure.",
            "recommended_recovery": "Retry failed agent stage.",
            "retry_allowed": False,
            "human_approval_required": True,
            "recovery_status": "RECOMMENDATION_ONLY",
            "source_references": [],
        }
        with (
            patch.object(
                case_workflow,
                "run_evidence_rag",
                side_effect=[RuntimeError("synthetic temporary failure"), evidence_result],
            ) as evidence_agent,
            patch.object(
                case_workflow,
                "predict_evidence_gaps",
                return_value={"available": False, "predictions": []},
            ) as evidence_prediction,
            patch.object(
                case_workflow, "run_sla_rag", return_value={"recommended_action": "Review."}
            ) as sla_agent,
            patch.object(
                case_workflow,
                "predict_case_sla_risk",
                return_value={"available": False},
            ) as sla_risk,
            patch.object(
                case_workflow, "run_escalation_rag", return_value=escalation_result
            ) as escalation_agent,
            patch.object(
                case_workflow, "run_recovery_rag", return_value=recovery_result
            ) as recovery_agent,
        ):
            first_response = self.client.post(
                "/api/agents/cases/PA-1042/workflow",
                json={"actor": "Test Reviewer"},
            )
            self.assertEqual(first_response.status_code, 200, first_response.text)
            self.assertEqual(
                first_response.json()["stages"]["evidence_agent"]["status"], "failed"
            )

            retry_response = self.client.post(
                "/api/agents/cases/PA-1042/workflow/retry",
                json={"actor": "Test Reviewer"},
            )

        self.assertEqual(retry_response.status_code, 200, retry_response.text)
        result = retry_response.json()
        self.assertEqual(result["stages"]["evidence_agent"]["status"], "completed")
        self.assertEqual(result["stages"]["evidence_prediction"]["status"], "completed")
        self.assertEqual(result["llm_explanation"]["engine"], case_workflow.DETERMINISTIC_EXPLANATION_ENGINE)
        self.assertIn("Case summary", result["llm_explanation"]["explanation"])
        self.assertEqual(evidence_agent.call_count, 2)
        evidence_prediction.assert_called_once()
        sla_agent.assert_called_once()
        sla_risk.assert_called_once()
        escalation_agent.assert_called_once()
        recovery_agent.assert_called_once()
        with database.connect() as db:
            stored = db.execute(
                "SELECT result_json FROM case_workflow_state WHERE case_id=?",
                ("PA-1042",),
            ).fetchone()
        self.assertEqual(
            json.loads(stored["result_json"])["stages"]["evidence_agent"]["status"],
            "completed",
        )

    def test_failed_agent_is_logged_and_recovery_stays_preview_only(self) -> None:
        with database.connect() as db:
            db.execute(
                """INSERT INTO workflow_failures
                   (case_id,occurred_at,error_code,error_details,workflow_state)
                   VALUES(?,?,?,?,?)""",
                ("PA-1042", "2026-10-10T00:00:00+00:00", "PRIOR_FAILURE", "synthetic", "test"),
            )
        recovery_result = {
            "case_id": "PA-1042",
            "failure_type": "PRIOR_FAILURE",
            "error_details": "Synthetic prior failure.",
            "likely_cause": "Test-only cause.",
            "recommended_recovery": "Review the failed stage.",
            "retry_allowed": False,
            "human_approval_required": True,
            "recovery_status": "RECOMMENDATION_ONLY",
            "source_references": [],
        }
        with (
            patch.object(case_workflow, "run_evidence_rag", side_effect=RuntimeError("synthetic agent error")),
            patch.object(case_workflow, "run_sla_rag", return_value={"recommended_action": "Review deadline."}),
            patch.object(case_workflow, "predict_case_sla_risk", side_effect=HTTPException(409, "deadline passed")),
            patch.object(case_workflow, "run_escalation_rag", return_value={"escalation_required": False, "escalation_level": "NONE", "reason": "No threshold.", "recommended_action": "Continue review.", "responsible_role": "", "approval_required": False, "source_references": [], "notification_status": "NOT_REQUIRED"}),
            patch.object(case_workflow, "run_recovery_rag", return_value=recovery_result) as recovery_agent,
        ):
            response = self.client.post(
                "/api/agents/cases/PA-1042/workflow",
                json={"actor": "Test Reviewer"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["workflow_status"], "partial")
        self.assertEqual(result["stages"]["evidence_agent"]["status"], "failed")
        self.assertEqual(result["stages"]["sla_risk_prediction"]["status"], "completed")
        risk_result = result["stages"]["sla_risk_prediction"]["result"]
        self.assertFalse(risk_result["available"])
        self.assertEqual(risk_result["operational_risk"]["risk_category"], "BREACHED")
        self.assertEqual(result["stages"]["recovery_agent"]["status"], "completed")
        self.assertEqual(result["stages"]["llm_explanation"]["status"], "completed")
        self.assertEqual(result["llm_explanation"]["engine"], case_workflow.DETERMINISTIC_EXPLANATION_ENGINE)
        recovery_agent.assert_called_once_with(
            "PA-1042",
            "Avery Patel",
            error_code=None,
            approve_recovery=False,
            preview_only=True,
        )
        with database.connect() as db:
            logged = db.execute(
                "SELECT COUNT(*) AS count FROM workflow_failures WHERE case_id=? AND error_code IN (?,?)",
                ("PA-1042", "CASE_WORKFLOW_EVIDENCE_AGENT", "CASE_WORKFLOW_LLM_EXPLANATION"),
            ).fetchone()["count"]
        self.assertEqual(logged, 1)

    def test_extracted_document_workflow_skips_ambiguous_deadline_then_runs_after_manager_correction(self) -> None:
        invalid_create = self.client.post(
            "/api/cases",
            json={
                "patient_reference": "SYNTHETIC-DEADLINE",
                "payer": "Northstar Health",
                "service": "Synthetic review",
                "deadline": "Oct 23, 6:30 PM",
                "coordinator": "Test Coordinator",
            },
        )
        self.assertEqual(invalid_create.status_code, 422, invalid_create.text)
        case_id = "PA-WORKFLOW-DEADLINE"
        self.add_northstar_case(case_id)
        with database.connect() as db:
            db.execute("UPDATE cases SET deadline=? WHERE id=?", ("Oct 23, 6:30 PM", case_id))
        upload = self.upload(
            "synthetic-workflow-notes.pdf",
            text_pdf("Synthetic clinical notes for a fictional authorization case."),
            case_id=case_id,
            submission_id="workflow-deadline-doc-1",
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["ocr_status"], "COMPLETED")
        invalid_correction = self.client.patch(
            f"/api/cases/{case_id}", json={"deadline": "Oct 24, 6:30 PM"}
        )
        self.assertEqual(invalid_correction.status_code, 422, invalid_correction.text)

        with (
            patch.object(agents, "explain_with_ollama", return_value=("Synthetic SLA rationale.", "test-local")),
            patch.object(escalation_agents, "explain_escalation_with_ollama", return_value=("Synthetic escalation rationale.", "test-local")),
        ):
            initial = self.client.post(
                f"/api/agents/cases/{case_id}/workflow", json={"actor": "Test Reviewer"}
            )
            self.assertEqual(initial.status_code, 200, initial.text)
            first_result = initial.json()
            self.assertEqual(first_result["stages"]["evidence_agent"]["status"], "completed")
            self.assertEqual(first_result["stages"]["sla_agent"]["status"], "skipped")
            self.assertEqual(first_result["stages"]["escalation_agent"]["status"], "skipped")
            self.assertEqual(first_result["stages"]["sla_risk_prediction"]["status"], "skipped")
            self.assertIn("ISO-8601", first_result["stages"]["sla_agent"]["reason"])
            self.assertEqual(first_result["workflow_status"], "partial")

            correction = self.client.patch(
                f"/api/cases/{case_id}", json={
                    "deadline": "2099-10-23T18:30:00+00:00",
                    "reason": "Corrected to a valid ISO-8601 deadline",
                    "expected_values": {"deadline": "Oct 23, 6:30 PM"},
                }
            )
            self.assertEqual(correction.status_code, 200, correction.text)
            self.assertEqual(correction.json()["deadline"], "2099-10-23T18:30:00+00:00")

            rerun = self.client.post(
                f"/api/agents/cases/{case_id}/workflow/retry", json={"actor": "Test Reviewer"}
            )
        self.assertEqual(rerun.status_code, 200, rerun.text)
        rerun_result = rerun.json()
        self.assertEqual(rerun_result["stages"]["sla_agent"]["status"], "completed")
        self.assertEqual(rerun_result["stages"]["escalation_agent"]["status"], "completed")
        self.assertEqual(rerun_result["stages"]["sla_risk_prediction"]["status"], "completed")
        self.assertEqual(rerun_result["workflow_status"], "completed")

        with database.connect() as db:
            skip_audits = db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE case_id=? AND action='CASE_WORKFLOW_STAGE_SKIPPED'",
                (case_id,),
            ).fetchone()[0]
            new_failures = db.execute(
                "SELECT COUNT(*) FROM workflow_failures WHERE case_id=?",
                (case_id,),
            ).fetchone()[0]
            docs = db.execute(
                "SELECT COUNT(*) FROM documents WHERE case_id=? AND submission_id=?",
                (case_id, "workflow-deadline-doc-1"),
            ).fetchone()[0]
        self.assertEqual(skip_audits, 3)
        self.assertEqual(new_failures, 0)
        self.assertEqual(docs, 1)

    def test_extracted_text_reaches_evidence_prediction(self) -> None:
        self.add_northstar_case("PA-PREDICT")
        response = self.upload(
            "synthetic-prediction-notes.pdf",
            text_pdf("Clinical notes describe the examination and treatment plan."),
            case_id="PA-PREDICT",
        )
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        observed_documents: list[dict] = []
        original_predict = main.predict_evidence_gaps

        def capture_prediction_input(required: object, documents: object, finding: object) -> dict:
            if isinstance(documents, list):
                observed_documents.extend(documents)
            return original_predict(required, documents, finding)

        with patch.object(main, "predict_evidence_gaps", side_effect=capture_prediction_input):
            prediction_response = self.client.post(
                "/api/agents/evidence/cases/PA-PREDICT/predict",
                json={},
            )

        self.assertEqual(prediction_response.status_code, 200, prediction_response.text)
        self.assertTrue(
            any(
                item.get("extracted_text") == document["extracted_text"]
                for item in observed_documents
            )
        )
        self.assertEqual(
            prediction_response.json()["prediction"]["predictions"][0]["verification_status"],
            "FOUND_IN_TEXT",
        )

    @unittest.skipUnless(TESSERACT_AVAILABLE, "Tesseract is not installed")
    def test_scanned_png_upload_runs_local_ocr(self) -> None:
        response = self.upload("synthetic-scan.png", scanned_png())

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ocr_status"], "COMPLETED")
        self.assertIn("clinical notes", document["extracted_text"].casefold())
        self.assertEqual(document["processing_status"], "OCR_COMPLETED")

    @unittest.skipUnless(TESSERACT_AVAILABLE, "Tesseract is not installed")
    def test_scanned_jpeg_upload_persists_and_returns_ocr_text(self) -> None:
        response = self.upload("synthetic-scan.jpg", scanned_jpeg())

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["original_filename"], "synthetic-scan.jpg")
        self.assertEqual(document["ocr_status"], "COMPLETED")
        self.assertIn("clinical notes", document["extracted_text"].casefold())
        stored_response = self.client.get(f"/api/documents/{document['id']}")
        self.assertEqual(stored_response.status_code, 200, stored_response.text)
        self.assertEqual(stored_response.json()["extracted_text"], document["extracted_text"])
        with database.connect() as db:
            stored = db.execute(
                "SELECT mime_type, extracted_text FROM documents WHERE id=?",
                (document["id"],),
            ).fetchone()
        self.assertEqual(stored["mime_type"], "image/jpeg")
        self.assertEqual(stored["extracted_text"], document["extracted_text"])

    @unittest.skipUnless(
        TESSERACT_AVAILABLE and POPPLER_AVAILABLE,
        "Local Tesseract and Poppler are required for scanned-PDF OCR",
    )
    def test_scanned_pdf_upload_runs_local_ocr(self) -> None:
        response = self.upload("synthetic-scan.pdf", scanned_pdf())

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ocr_status"], "COMPLETED")
        self.assertIn("clinical notes", document["extracted_text"].casefold())
        self.assertEqual(document["processing_status"], "OCR_COMPLETED")

    @unittest.skipUnless(
        TESSERACT_AVAILABLE and POPPLER_AVAILABLE,
        "Local Tesseract and Poppler are required to test OCR of a blank scanned page",
    )
    def test_blank_pdf_does_not_return_null_or_claim_success(self) -> None:
        response = self.upload("synthetic-blank.pdf", text_pdf(""))

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ocr_status"], "FAILED")
        self.assertEqual(document["processing_status"], "OCR_FAILED")
        self.assertEqual(document["extracted_text"], "")
        self.assertIn("No readable text", document["processing_error"])
        with database.connect() as db:
            stored = dict(
                db.execute("SELECT * FROM documents WHERE id=?", (document["id"],)).fetchone()
            )
        self.assertEqual(stored["extracted_text"], "")
        self.assertEqual(stored["ocr_status"], "FAILED")

    def test_corrupted_pdf_returns_safe_actionable_processing_error(self) -> None:
        response = self.upload("synthetic-corrupt.pdf", b"%PDF-1.7\nnot a valid PDF")

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ocr_status"], "FAILED")
        self.assertEqual(document["extracted_text"], "")
        self.assertIn("could not be read", document["processing_error"])
        self.assertNotIn(str(self.storage_root), document["processing_error"])

    def test_failed_retry_clears_previous_text_and_hides_parser_details(self) -> None:
        response = self.upload(
            "synthetic-retry.pdf",
            text_pdf("Clinical notes contain synthetic, non-sensitive text."),
        )
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertTrue(document["extracted_text"])

        with patch.object(
            main,
            "_extract_text_from_file",
            side_effect=RuntimeError("Parser failure contained synthetic PHI"),
        ):
            retry_response = self.client.post(f"/api/documents/{document['id']}/process")

        self.assertEqual(retry_response.status_code, 200, retry_response.text)
        retried = retry_response.json()
        self.assertEqual(retried["ocr_status"], "FAILED")
        self.assertEqual(retried["extracted_text"], "")
        self.assertNotIn("synthetic PHI", retried["processing_error"])
        self.assertEqual(retried["processing_error"], (
            "The document could not be read or text extraction failed. Verify that it is "
            "a valid, unencrypted PDF or image, then retry or request human review."
        ))

    def test_ocr_unavailable_is_persisted_as_failed_without_marking_evidence_missing(self) -> None:
        with patch.object(main.pytesseract, "get_tesseract_version", side_effect=TesseractNotFoundError()):
            response = self.upload("synthetic-scan.png", scanned_png())

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ocr_status"], "FAILED")
        self.assertEqual(document["processing_status"], "OCR_FAILED")
        self.assertIn("TESSERACT_PATH", document["processing_error"])
        finding_response = self.client.post("/api/agents/evidence/cases/PA-1042/review", json={})
        self.assertEqual(finding_response.status_code, 200, finding_response.text)
        finding = finding_response.json()["finding"]
        self.assertEqual(finding["missing_documents"], [])
        unreadable = next(
            item
            for item in finding["unreadable_documents"]
            if item["document_name"] == "synthetic-scan.png"
        )
        self.assertEqual(unreadable["category"], "Clinical Notes")
        self.assertEqual(unreadable["ocr_status"], "FAILED")
        self.assertTrue(finding["needs_human_review"])

    @unittest.skipUnless(TESSERACT_AVAILABLE, "Tesseract is not installed")
    def test_scanned_pdf_without_poppler_is_saved_with_actionable_error(self) -> None:
        with patch.object(
            main,
            "convert_from_path",
            side_effect=PDFInfoNotInstalledError("Poppler is unavailable"),
        ):
            response = self.upload("synthetic-scan.pdf", scanned_pdf())

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ocr_status"], "FAILED")
        self.assertIn("Scanned PDF OCR requires Poppler", document["processing_error"])
        self.assertIn("POPPLER_PATH", document["processing_error"])

    def test_upload_rejects_invalid_extension_and_mismatched_content(self) -> None:
        unsupported = self.upload("notes.txt", b"not a supported document")
        mismatch = self.upload("notes.pdf", b"\x89PNG\r\n\x1a\nnot-a-real-png")

        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("filename extension", mismatch.json()["detail"])

    def test_imaging_report_category_is_supported(self) -> None:
        response = self.upload(
            "synthetic-imaging.pdf",
            text_pdf("Synthetic imaging report for a fictional case."),
            category="Imaging Report",
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["category"], "Imaging Report")

    def test_requested_categories_are_saved_per_document_without_overwriting(self) -> None:
        categories = (
            "Clinical Reports",
            "Imaging Reports",
            "Treatment Reports",
            "Medical Necessity Documents",
            "Prescription",
        )
        uploaded = [
            self.upload(
                f"synthetic-category-{index}.pdf",
                text_pdf(f"Synthetic category report {index} for this fictional case."),
                category=category,
            )
            for index, category in enumerate(categories, start=1)
        ]

        for response in uploaded:
            self.assertEqual(response.status_code, 200, response.text)

        documents = [response.json() for response in uploaded]
        self.assertEqual([item["category"] for item in documents], list(categories))
        self.assertEqual(len({item["id"] for item in documents}), len(categories))
        with database.connect() as db:
            stored_categories = [
                row["category"]
                for row in db.execute(
                    "SELECT category FROM documents WHERE case_id='PA-1042' "
                    "AND original_filename LIKE 'synthetic-category-%' ORDER BY id"
                )
            ]
        self.assertEqual(stored_categories, list(categories))

    def test_evidence_match_carries_category_but_category_alone_does_not_prove_evidence(self) -> None:
        response = self.upload(
            "synthetic-cardiology-report.pdf",
            text_pdf(
                "A specialist letter documents the fictional patient's symptoms "
                "and cardiac CT recommendation."
            ),
            category="Clinical Reports",
            case_id="PA-1039",
        )
        self.assertEqual(response.status_code, 200, response.text)

        finding_response = self.client.post(
            "/api/agents/evidence/cases/PA-1039/review", json={}
        )
        self.assertEqual(finding_response.status_code, 200, finding_response.text)
        finding = finding_response.json()["finding"]
        match = next(
            item
            for item in finding["found_documents"]
            if item["required_document"] == "Specialist letter"
        )
        self.assertEqual(match["category"], "Clinical Reports")
        self.assertEqual(match["ocr_status"], "COMPLETED")
        self.assertEqual(finding["possible_mismatches"], [])

        self.add_northstar_cardiac_ct_case("PA-CT-CATEGORY")
        category_only_response = self.upload(
            "synthetic-imaging-category-only.pdf",
            text_pdf("This fictional document contains unrelated administrative text."),
            category="Imaging Reports",
            case_id="PA-CT-CATEGORY",
        )
        self.assertEqual(category_only_response.status_code, 200, category_only_response.text)
        category_only_finding = self.client.post(
            "/api/agents/evidence/cases/PA-CT-CATEGORY/review", json={}
        ).json()["finding"]
        self.assertNotIn(
            "Specialist letter",
            [
                item["required_document"]
                for item in category_only_finding["found_documents"]
            ],
        )

    def test_upload_rejects_oversize_file_and_nonexistent_case(self) -> None:
        oversized = self.upload("large.pdf", b"%PDF-" + b"x" * (main.MAX_UPLOAD_BYTES + 1))
        missing_case = self.upload("valid.pdf", text_pdf("Synthetic notes."), case_id="PA-9999")

        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(missing_case.status_code, 404)

    def test_evidence_agent_reports_page_excerpt_and_only_marks_missing_after_successful_ocr(self) -> None:
        self.add_northstar_case("PA-SYNTHETIC")
        response = self.upload(
            "synthetic-notes.pdf",
            text_pdf("Clinical notes are attached for the synthetic MRI case."),
            case_id="PA-SYNTHETIC",
        )

        self.assertEqual(response.status_code, 200, response.text)
        finding_response = self.client.post(
            "/api/agents/evidence/cases/PA-SYNTHETIC/review",
            json={},
        )
        self.assertEqual(finding_response.status_code, 200, finding_response.text)
        finding = finding_response.json()["finding"]
        self.assertEqual(finding["verification_status"], "NEEDS_REVIEW")
        self.assertEqual(finding["missing_documents"], ["Conservative treatment history"])
        self.assertEqual(finding["found_documents"][0]["required_document"], "Clinical notes")
        self.assertEqual(finding["found_documents"][0]["document_name"], "synthetic-notes.pdf")
        self.assertEqual(finding["found_documents"][0]["page_number"], 1)
        self.assertIn("Clinical notes", finding["found_documents"][0]["excerpt"])
        self.assertTrue(finding["needs_human_review"])
        self.assertNotIn("approve", finding["recommended_action"].lower())
        self.assertNotIn("deny", finding["recommended_action"].lower())

    def test_evidence_agent_separates_possible_matches_from_exact_text_matches(self) -> None:
        self.add_northstar_case("PA-POSSIBLE")
        response = self.upload(
            "synthetic-possible-match.pdf",
            text_pdf("Clinical information is included in this synthetic case record."),
            case_id="PA-POSSIBLE",
        )

        self.assertEqual(response.status_code, 200, response.text)
        finding_response = self.client.post(
            "/api/agents/evidence/cases/PA-POSSIBLE/review",
            json={},
        )
        self.assertEqual(finding_response.status_code, 200, finding_response.text)
        finding = finding_response.json()["finding"]
        self.assertEqual(finding["found_documents"], [])
        self.assertEqual(
            finding["possible_evidence_matches"][0]["required_document"],
            "Clinical notes",
        )
        self.assertEqual(finding["possible_evidence_matches"][0]["page_number"], 1)
        self.assertTrue(finding["needs_human_review"])


if __name__ == "__main__":
    unittest.main()
