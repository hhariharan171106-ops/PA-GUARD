import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pdf2image.exceptions import PDFInfoNotInstalledError
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from pytesseract.pytesseract import TesseractNotFoundError

import database
import evidence_agents
import main


TESSERACT_AVAILABLE = bool(
    shutil.which("tesseract")
    or Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").is_file()
    or Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe").is_file()
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
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
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

    def upload(self, filename: str, content: bytes, category: str = "Clinical Notes", case_id: str = "PA-1042"):
        return self.client.post(
            f"/api/cases/{case_id}/documents",
            files={"file": (filename, content, "application/octet-stream")},
            data={"category": category},
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

    @unittest.skipUnless(TESSERACT_AVAILABLE, "Tesseract is not installed")
    def test_scanned_png_upload_runs_local_ocr(self) -> None:
        response = self.upload("synthetic-scan.png", scanned_png())

        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertEqual(document["ocr_status"], "COMPLETED")
        self.assertIn("clinical notes", document["extracted_text"].casefold())
        self.assertEqual(document["processing_status"], "OCR_COMPLETED")

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
        self.assertTrue(any(item["document_name"] == "synthetic-scan.png" for item in finding["unreadable_documents"]))
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
