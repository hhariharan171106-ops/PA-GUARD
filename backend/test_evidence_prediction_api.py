import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import database
import evidence_agents
import main


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class EvidencePredictionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        temporary_root = Path(self.temp_directory.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DB_PATH", temporary_root / "test.db")
        )
        self.stack.enter_context(
            patch.object(database, "DOCUMENT_STORAGE_DIR", temporary_root / "documents")
        )

        def closing_connect():
            connection = sqlite3.connect(database.DB_PATH, factory=ClosingConnection)
            connection.row_factory = sqlite3.Row
            return connection

        for module in (database, main, evidence_agents):
            self.stack.enter_context(patch.object(module, "connect", closing_connect))
        self.stack.enter_context(patch.object(main, "index_policies", lambda: {"indexed": 0}))
        self.stack.enter_context(
            patch.object(evidence_agents, "embed_text", lambda _text: ([1.0], "test-local"))
        )
        self.stack.enter_context(
            patch.object(
                evidence_agents,
                "explain_evidence_with_ollama",
                lambda _facts: ("Human review is required.", "test-local"),
            )
        )
        self.client = self.stack.enter_context(TestClient(main.app))
        self.addCleanup(self.stack.close)

    def add_case_and_document(self) -> None:
        with database.connect() as db:
            db.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "PA-PREDICT",
                    "SYNTHETIC-CASE",
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
            db.execute(
                """INSERT INTO documents
                   (case_id,name,status,category,ocr_status,extracted_text)
                   VALUES(?,?,?,?,?,?)""",
                (
                    "PA-PREDICT",
                    "Clinical Notes",
                    "RECEIVED",
                    "Clinical Notes",
                    "COMPLETED",
                    "Clinical notes describe the examination and treatment plan.",
                ),
            )

    def test_prediction_endpoint_returns_existing_finding_and_separate_predictions(self) -> None:
        self.add_case_and_document()

        response = self.client.post(
            "/api/agents/evidence/cases/PA-PREDICT/predict",
            json={},
        )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["mode"], "experimental_prediction_only")
        self.assertIn("finding", result)
        self.assertEqual(
            result["finding"]["required_documents"],
            ["Clinical notes", "Conservative treatment history"],
        )
        prediction = result["prediction"]
        self.assertTrue(prediction["experimental"])
        self.assertFalse(prediction["validated"])
        self.assertEqual(len(prediction["predictions"]), 2)
        self.assertEqual(
            prediction["predictions"][0]["verification_status"],
            "FOUND_IN_TEXT",
        )
        self.assertEqual(
            prediction["predictions"][1]["verification_status"],
            "VERIFIED_MISSING",
        )
        self.assertIn(
            prediction["predictions"][1]["ml_prediction"],
            {"PREDICTED_MISSING", "NOT_PREDICTED_MISSING"},
        )

    def test_prediction_endpoint_reports_unavailable_requirements_explicitly(self) -> None:
        self.add_case_and_document()
        with patch.object(
            main,
            "run_evidence_rag",
            return_value={
                "case_id": "PA-PREDICT",
                "required_documents": [],
                "found_documents": [],
                "possible_evidence_matches": [],
                "missing_documents": [],
                "unreadable_documents": [],
                "possible_mismatches": ["No approved payer requirements were retrieved."],
                "verification_status": "NEEDS_REVIEW",
                "explanation": "Requirements unavailable.",
                "recommended_action": "Route to a human reviewer.",
                "source_references": [],
                "needs_human_review": True,
            },
        ):
            response = self.client.post(
                "/api/agents/evidence/cases/PA-PREDICT/predict",
                json={},
            )

        self.assertEqual(response.status_code, 200, response.text)
        prediction = response.json()["prediction"]
        self.assertFalse(prediction["requirements_available"])
        self.assertFalse(prediction["available"])
        self.assertEqual(prediction["predictions"], [])

    def test_prediction_endpoint_returns_not_found_for_unknown_case(self) -> None:
        response = self.client.post(
            "/api/agents/evidence/cases/PA-UNKNOWN/predict",
            json={},
        )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
