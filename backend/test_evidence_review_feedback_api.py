import csv
import io
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import database
import main


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class EvidenceReviewFeedbackApiTests(unittest.TestCase):
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
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        for module in (database, main):
            self.stack.enter_context(patch.object(module, "connect", closing_connect))
        self.stack.enter_context(
            patch.object(main, "index_policies", lambda: {"indexed": 0})
        )
        self.client = self.stack.enter_context(TestClient(main.app))
        self.addCleanup(self.stack.close)

    def submit(self, evidence: str = "Clinical notes", prediction: str | None = None):
        payload = {"required_evidence": evidence}
        if prediction is not None:
            payload["ml_prediction"] = prediction
        return self.client.post(
            "/api/cases/PA-1042/evidence-review-feedback",
            json=payload,
        )

    def decide(
        self,
        feedback_id: int,
        status: str = "CONFIRMED",
        outcome: str | None = "PRESENT",
    ):
        payload = {
            "review_status": status,
            "reviewer_identifier": "reviewer-test-17",
        }
        if outcome is not None:
            payload["human_outcome"] = outcome
        return self.client.post(
            f"/api/evidence-review-feedback/{feedback_id}/review",
            json=payload,
        )

    def test_saving_feedback_keeps_prediction_separate_and_starts_pending(self) -> None:
        response = self.submit(prediction="PREDICTED_MISSING")

        self.assertEqual(response.status_code, 200, response.text)
        feedback = response.json()
        self.assertEqual(feedback["case_id"], "PA-1042")
        self.assertEqual(feedback["required_evidence"], "Clinical notes")
        self.assertEqual(feedback["ml_prediction"], "PREDICTED_MISSING")
        self.assertEqual(feedback["human_outcome"], "UNKNOWN")
        self.assertEqual(feedback["review_status"], "PENDING")
        self.assertIsNone(feedback["reviewer_identifier"])
        self.assertIsNone(feedback["reviewed_at"])
        listed = self.client.get("/api/cases/PA-1042/evidence-review-feedback")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json(), [feedback])

    def test_invalid_case_id_is_rejected(self) -> None:
        response = self.client.post(
            "/api/cases/PA-NOT-FOUND/evidence-review-feedback",
            json={"required_evidence": "Clinical notes"},
        )
        self.assertEqual(response.status_code, 404)

    def test_evidence_not_in_applicable_approved_requirement_is_rejected(self) -> None:
        response = self.submit("Unapproved evidence name")
        self.assertEqual(response.status_code, 422)

    def test_present_and_missing_are_only_set_by_review_decision(self) -> None:
        for outcome in ("PRESENT", "MISSING"):
            with self.subTest(outcome=outcome):
                submitted = self.submit()
                self.assertEqual(submitted.status_code, 200, submitted.text)
                reviewed = self.decide(submitted.json()["id"], outcome=outcome)
                self.assertEqual(reviewed.status_code, 200, reviewed.text)
                self.assertEqual(reviewed.json()["human_outcome"], outcome)
                self.assertEqual(reviewed.json()["review_status"], "CONFIRMED")
                self.assertEqual(
                    reviewed.json()["reviewer_identifier"], "reviewer-test-17"
                )

    def test_unknown_and_needs_review_outcomes_are_excluded(self) -> None:
        for outcome in ("UNKNOWN", "NEEDS_REVIEW"):
            with self.subTest(outcome=outcome):
                submitted = self.submit()
                reviewed = self.decide(submitted.json()["id"], outcome=outcome)
                self.assertEqual(reviewed.status_code, 200, reviewed.text)

        export = self.client.get(
            "/api/evidence-review-feedback/training-export?format=json"
        )
        self.assertEqual(export.status_code, 200, export.text)
        self.assertEqual(export.json(), {"count": 0, "records": []})

    def test_pending_and_rejected_feedback_are_excluded(self) -> None:
        pending = self.submit()
        rejected = self.submit()
        rejected_result = self.decide(
            rejected.json()["id"], status="REJECTED", outcome=None
        )
        self.assertEqual(rejected_result.status_code, 200, rejected_result.text)
        self.assertEqual(rejected_result.json()["review_status"], "REJECTED")

        export = self.client.get(
            "/api/evidence-review-feedback/training-export?format=json"
        )
        self.assertEqual(export.json(), {"count": 0, "records": []})
        self.assertEqual(pending.json()["review_status"], "PENDING")

    def test_confirmed_training_export_contains_only_allowlisted_fields(self) -> None:
        submitted = self.submit(prediction="NOT_PREDICTED_MISSING")
        reviewed = self.decide(submitted.json()["id"], outcome="MISSING")
        self.assertEqual(reviewed.status_code, 200, reviewed.text)

        json_export = self.client.get(
            "/api/evidence-review-feedback/training-export?format=json"
        )
        self.assertEqual(json_export.status_code, 200, json_export.text)
        result = json_export.json()
        self.assertEqual(result["count"], 1)
        record = result["records"][0]
        self.assertEqual(record["human_outcome"], "MISSING")
        self.assertEqual(record["required_evidence"], "Clinical notes")
        self.assertEqual(
            set(record),
            {
                "id",
                "required_evidence",
                "ml_prediction",
                "human_outcome",
                "created_at",
                "reviewed_at",
            },
        )
        self.assertNotIn("case_id", record)
        self.assertNotIn("reviewer_identifier", record)
        self.assertNotIn("patient_reference", record)
        self.assertNotIn("extracted_text", record)

        csv_export = self.client.get(
            "/api/evidence-review-feedback/training-export?format=csv"
        )
        self.assertEqual(csv_export.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(csv_export.text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["human_outcome"], "MISSING")
        self.assertEqual(rows[0]["required_evidence"], "Clinical notes")

    def test_non_pending_feedback_cannot_be_reviewed_again(self) -> None:
        submitted = self.submit()
        feedback_id = submitted.json()["id"]
        self.assertEqual(self.decide(feedback_id).status_code, 200)
        repeated = self.decide(feedback_id, outcome="MISSING")
        self.assertEqual(repeated.status_code, 409)

    def test_database_initialization_preserves_existing_records(self) -> None:
        with database.connect() as db:
            original_cases = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            original_documents = db.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
            known_case = db.execute(
                "SELECT patient_reference FROM cases WHERE id='PA-1042'"
            ).fetchone()[0]
            db.execute("DROP TABLE evidence_review_feedback")

        database.initialize()

        with database.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
                original_cases,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                original_documents,
            )
            self.assertEqual(
                db.execute(
                    "SELECT patient_reference FROM cases WHERE id='PA-1042'"
                ).fetchone()[0],
                known_case,
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='evidence_review_feedback'"
                ).fetchone()
            )

    def test_feedback_case_foreign_key_is_enforced(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with database.connect() as db:
                db.execute(
                    """INSERT INTO evidence_review_feedback
                       (case_id,required_evidence,created_at)
                       VALUES('PA-NOT-FOUND','Clinical notes','2026-10-18T00:00:00+00:00')"""
                )


if __name__ == "__main__":
    unittest.main()
