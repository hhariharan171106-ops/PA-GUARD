import unittest
from unittest.mock import patch

import evidence_prediction_model as prediction_model


def finding(
    *,
    found: list[str] | None = None,
    possible: list[str] | None = None,
    missing: list[str] | None = None,
    unreadable: list[dict] | None = None,
) -> dict:
    return {
        "found_documents": [{"required_document": item} for item in found or []],
        "possible_evidence_matches": [
            {"required_document": item} for item in possible or []
        ],
        "missing_documents": missing or [],
        "unreadable_documents": unreadable or [],
    }


class EvidencePredictionModelTests(unittest.TestCase):
    def setUp(self) -> None:
        prediction_model._get_model.cache_clear()

    def tearDown(self) -> None:
        prediction_model._get_model.cache_clear()

    def test_found_text_remains_separate_from_experimental_prediction(self) -> None:
        result = prediction_model.predict_evidence_gaps(
            ["Clinical notes"],
            [
                {
                    "category": "Clinical Notes",
                    "ocr_status": "COMPLETED",
                    "extracted_text": "Clinical notes describe the symptoms and treatment plan.",
                }
            ],
            finding(found=["Clinical notes"]),
        )

        self.assertTrue(result["available"])
        self.assertTrue(result["experimental"])
        self.assertFalse(result["validated"])
        self.assertEqual(result["predictions"][0]["verification_status"], "FOUND_IN_TEXT")
        self.assertIn(
            result["predictions"][0]["ml_prediction"],
            {"PREDICTED_MISSING", "NOT_PREDICTED_MISSING"},
        )

    def test_potentially_missing_category_is_predicted_without_claiming_verification(self) -> None:
        result = prediction_model.predict_evidence_gaps(
            ["Conservative treatment history"],
            [
                {
                    "category": "Clinical Notes",
                    "ocr_status": "COMPLETED",
                    "extracted_text": "Clinical notes describe the examination and diagnosis.",
                }
            ],
            finding(missing=["Conservative treatment history"]),
        )

        prediction = result["predictions"][0]
        self.assertEqual(prediction["ml_prediction"], "PREDICTED_MISSING")
        self.assertEqual(prediction["verification_status"], "VERIFIED_MISSING")
        self.assertTrue(result["experimental"])

    def test_possible_agent_match_remains_needs_review_not_verified_missing(self) -> None:
        result = prediction_model.predict_evidence_gaps(
            ["Clinical notes"],
            [
                {
                    "category": "Treatment History",
                    "ocr_status": "COMPLETED",
                    "extracted_text": "Clinical information is included with the current diagnosis.",
                }
            ],
            finding(possible=["Clinical notes"]),
        )

        prediction = result["predictions"][0]
        self.assertEqual(prediction["verification_status"], "NEEDS_REVIEW")
        self.assertNotEqual(prediction["verification_status"], prediction["ml_prediction"])

    def test_ocr_failure_and_unreadable_text_are_unknown_not_missing(self) -> None:
        for document in (
            {
                "category": "Clinical Notes",
                "ocr_status": "FAILED",
                "extracted_text": "",
            },
            {
                "category": "Clinical Notes",
                "ocr_status": "COMPLETED",
                "extracted_text": "too short",
            },
        ):
            with self.subTest(document=document):
                result = prediction_model.predict_evidence_gaps(
                    ["Clinical notes"],
                    [document],
                    finding(missing=["Clinical notes"]),
                )
                self.assertFalse(result["available"])
                self.assertEqual(result["predictions"][0]["ml_prediction"], "UNKNOWN")
                self.assertEqual(result["predictions"][0]["verification_status"], "UNKNOWN")

    def test_unreadable_agent_finding_blocks_absence_prediction(self) -> None:
        result = prediction_model.predict_evidence_gaps(
            ["Clinical notes"],
            [
                {
                    "category": "Treatment History",
                    "ocr_status": "COMPLETED",
                    "extracted_text": "Treatment history includes a series of prior therapeutic visits.",
                }
            ],
            finding(
                missing=["Clinical notes"],
                unreadable=[{"document_name": "scan.pdf", "reason": "OCR failed"}],
            ),
        )

        self.assertEqual(result["predictions"][0]["verification_status"], "UNKNOWN")
        self.assertEqual(result["predictions"][0]["ml_prediction"], "UNKNOWN")

    def test_no_applicable_requirements_returns_explicit_unavailable_result(self) -> None:
        result = prediction_model.predict_evidence_gaps([], [], finding())

        self.assertFalse(result["available"])
        self.assertFalse(result["requirements_available"])
        self.assertEqual(result["predictions"], [])
        self.assertIn("No applicable approved", result["message"])

    def test_model_unavailable_is_explicit_and_does_not_change_verification(self) -> None:
        with patch.object(
            prediction_model,
            "_get_model",
            side_effect=prediction_model.ModelUnavailableError("test model unavailable"),
        ):
            result = prediction_model.predict_evidence_gaps(
                ["Clinical notes"],
                [
                    {
                        "category": "Clinical Notes",
                        "ocr_status": "COMPLETED",
                        "extracted_text": "Clinical notes describe the symptoms and treatment plan.",
                    }
                ],
                finding(found=["Clinical notes"]),
            )

        self.assertFalse(result["available"])
        self.assertIn("test model unavailable", result["message"])
        self.assertEqual(result["predictions"][0]["ml_prediction"], "UNKNOWN")
        self.assertEqual(result["predictions"][0]["verification_status"], "FOUND_IN_TEXT")

    def test_invalid_input_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            prediction_model.predict_evidence_gaps(
                ["Clinical notes"], None, finding()
            )


if __name__ == "__main__":
    unittest.main()
