import csv
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import joblib
from fastapi.testclient import TestClient

import database
import main
from sla_risk_model import (
    FEATURE_NAMES,
    InvalidRiskFeatures,
    MODEL_VERSION,
    ModelUnavailableError,
    SlaRiskModel,
    create_pipeline,
)
from sla_risk_synthetic_data import (
    generate_synthetic_dataset,
    simulate_synthetic_case,
    write_synthetic_csv,
)
from train_sla_risk_model import (
    EXPERIMENTAL_MODEL_VERSION,
    split_synthetic_dataset,
    train_and_evaluate,
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def create_test_v2_artifact(model_path: Path) -> Path:
    rows, labels = generate_synthetic_dataset(1200, seed=31)
    pipeline = create_pipeline()
    pipeline.fit(rows, labels)
    joblib.dump(
        {
            "model_version": MODEL_VERSION,
            "feature_names": FEATURE_NAMES,
            "training_data_source": "synthetic test fixture",
            "threshold": 0.35,
            "pipeline": pipeline,
        },
        model_path,
    )
    return model_path


class SlaRiskModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.production_format_path = create_test_v2_artifact(
            Path(cls.temp_directory.name) / "risk-model-v2.joblib"
        )
        cls.model_path = Path(cls.temp_directory.name) / "risk-model-experimental.joblib"
        cls.report = train_and_evaluate(
            cls.model_path,
            sample_count=1200,
            json_report_path=None,
            markdown_report_path=None,
        )
        cls.model = SlaRiskModel(cls.model_path)
        cls.compatible_model = SlaRiskModel(cls.production_format_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_directory.cleanup()

    def setUp(self) -> None:
        self.features = {
            "hours_remaining": 12.5,
            "payer": "Northstar Health",
            "service": "Lumbar MRI",
            "case_status": "EVIDENCE REVIEW",
            "unresolved_document_count": 2,
            "workflow_failure_count": 1,
            "pending_task_count": 1,
        }

    def test_model_trains_saves_and_reloads(self) -> None:
        prediction = self.compatible_model.predict(self.features, "PA-TEST")
        self.assertTrue(self.model_path.is_file())
        reloaded = SlaRiskModel(self.production_format_path).predict(
            self.features, "PA-TEST"
        )
        self.assertEqual(prediction["predicted_sla_breach"], reloaded["predicted_sla_breach"])
        self.assertEqual(prediction["breach_probability"], reloaded["breach_probability"])
        self.assertEqual(prediction["model_version"], reloaded["model_version"])
        self.assertFalse(prediction["reliable"])
        self.assertIn("synthetic", prediction["reliability_note"].lower())
        experimental_artifact = joblib.load(self.model_path)
        self.assertEqual(
            experimental_artifact["model_version"], EXPERIMENTAL_MODEL_VERSION
        )
        self.assertEqual(experimental_artifact["threshold"], self.report["threshold_selection"]["threshold"])

    def test_prediction_reports_risk_and_available_factors(self) -> None:
        prediction = self.compatible_model.predict(self.features, "PA-TEST")
        self.assertTrue(prediction["available"])
        self.assertIn(prediction["risk_level"], {"LOW", "MEDIUM", "HIGH"})
        self.assertGreaterEqual(prediction["risk_score"], 0)
        self.assertLessEqual(prediction["risk_score"], 100)
        self.assertGreaterEqual(prediction["breach_probability"], 0)
        self.assertLessEqual(prediction["breach_probability"], 1)
        self.assertIsInstance(prediction["predicted_sla_breach"], bool)
        self.assertEqual(prediction["model_version"], "sla-breach-random-forest-v2")
        self.assertTrue(prediction["factors"])
        self.assertIn("global feature importance", prediction["explanation"])

    def test_missing_or_invalid_features_are_rejected(self) -> None:
        incomplete = dict(self.features)
        del incomplete["case_status"]
        with self.assertRaisesRegex(InvalidRiskFeatures, "Missing required features"):
            self.compatible_model.predict(incomplete, "PA-TEST")

        invalid = {**self.features, "workflow_failure_count": -1}
        with self.assertRaisesRegex(InvalidRiskFeatures, "non-negative integer"):
            self.compatible_model.predict(invalid, "PA-TEST")

        invalid = {**self.features, "hours_remaining": float("nan")}
        with self.assertRaisesRegex(InvalidRiskFeatures, "finite number"):
            self.compatible_model.predict(invalid, "PA-TEST")

        invalid = {**self.features, "hours_remaining": 0}
        with self.assertRaisesRegex(InvalidRiskFeatures, "must be positive"):
            self.compatible_model.predict(invalid, "PA-TEST")

    def test_invalid_case_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidRiskFeatures, "case_id"):
            self.compatible_model.predict(self.features, "")

    def test_missing_model_artifact_does_not_train_automatically(self) -> None:
        missing_path = Path(self.temp_directory.name) / "missing.joblib"
        with self.assertRaisesRegex(ModelUnavailableError, "artifact is missing"):
            SlaRiskModel(missing_path).predict(self.features, "PA-TEST")
        self.assertFalse(missing_path.exists())

    def test_training_refuses_to_overwrite_an_existing_artifact(self) -> None:
        existing_path = Path(self.temp_directory.name) / "existing.joblib"
        existing_path.write_bytes(b"preserve-existing-model")
        with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
            train_and_evaluate(existing_path, sample_count=1200)
        self.assertEqual(existing_path.read_bytes(), b"preserve-existing-model")

    def test_unfitted_model_artifact_is_rejected(self) -> None:
        invalid_path = Path(self.temp_directory.name) / "unfitted.joblib"
        joblib.dump(
            {
                "model_version": "sla-breach-random-forest-v2",
                "feature_names": FEATURE_NAMES,
                "pipeline": create_pipeline(),
            },
            invalid_path,
        )
        with self.assertRaisesRegex(ModelUnavailableError, "has not been fitted"):
            SlaRiskModel(invalid_path).predict(self.features, "PA-TEST")

    def test_incompatible_model_artifact_is_rejected(self) -> None:
        invalid_path = Path(self.temp_directory.name) / "incompatible.joblib"
        joblib.dump(
            {
                "model_version": EXPERIMENTAL_MODEL_VERSION,
                "feature_names": FEATURE_NAMES,
                "pipeline": create_pipeline(),
            },
            invalid_path,
        )
        with self.assertRaisesRegex(ModelUnavailableError, "unsupported format"):
            SlaRiskModel(invalid_path).predict(self.features, "PA-TEST")

    def test_training_report_uses_stratified_holdout_and_baseline_metrics(self) -> None:
        self.assertEqual(self.report["dataset"]["sample_count"], 1200)
        self.assertEqual(self.report["dataset"]["exact_duplicate_feature_rows"], 0)
        self.assertFalse(
            self.report["evaluation_protocol"]["test_used_for_threshold_selection"]
        )
        self.assertEqual(
            self.report["evaluation_protocol"]["preprocessing_fit_partition"],
            "train only",
        )
        for threshold_key in (
            "threshold_selection",
            "baseline_prior_only_threshold_selection",
            "hours_only_threshold_selection",
        ):
            self.assertTrue(self.report[threshold_key]["recall_target_met"])
            self.assertGreaterEqual(
                self.report[threshold_key]["validation_recall_breach"],
                self.report[threshold_key]["recall_target"],
            )
        self.assertEqual(self.report["split_counts"], {
            "train": 720,
            "validation": 240,
            "test": 240,
        })
        self.assertEqual(
            set(self.report["feature_ablation_test"]),
            {
                "payer",
                "service",
                "case_status",
                "unresolved_document_count",
                "pending_task_count",
                "workflow_failure_count",
            },
        )
        for ablation in self.report["feature_ablation_test"].values():
            self.assertEqual(
                ablation["test"]["confusion_matrix_labels_0_1"][1][0],
                ablation["test"]["false_negative"],
            )
            self.assertIn(
                "roc_auc", ablation["change_when_feature_is_removed"]
            )
        self.assertEqual(
            self.report["threshold_selection"]["recall_target_met"],
            True,
        )
        self.assertGreaterEqual(
            self.report["threshold_selection"]["validation_recall_breach"],
            self.report["threshold_selection"]["recall_target"],
        )
        for name in ("accuracy", "precision_breach", "recall_breach", "f1_breach"):
            self.assertIn(name, self.report["random_forest_test"])
            self.assertIn(name, self.report["baseline_prior_only_test"])
        for name in ("roc_auc", "pr_auc_average_precision", "brier_score"):
            self.assertIn(name, self.report["random_forest_test"])
        self.assertEqual(
            self.report["random_forest_test"]["false_negative"],
            self.report["random_forest_test"]["confusion_matrix_labels_0_1"][1][0],
        )
        self.assertEqual(
            sum(
                item["sample_count"]
                for item in self.report["random_forest_test"]["calibration_bins"]
            ),
            self.report["split_counts"]["test"],
        )
        self.assertIn("hours_only_test", self.report)
        self.assertEqual(
            sum(
                item["sample_count"]
                for item in self.report["baseline_prior_only_test"]["calibration_bins"]
            ),
            self.report["split_counts"]["test"],
        )
        self.assertEqual(
            len(self.report["random_forest_test"]["confusion_matrix_labels_0_1"]),
            2,
        )

    def test_synthetic_dataset_is_reproducible_and_omits_simulated_duration(self) -> None:
        first_rows, first_labels = generate_synthetic_dataset(100, seed=17)
        second_rows, second_labels = generate_synthetic_dataset(100, seed=17)
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first_labels, second_labels)
        self.assertTrue(set(first_labels).issubset({0, 1}))
        self.assertNotIn("simulated_resolution_hours", first_rows[0])
        self.assertNotIn("sla_breach", first_rows[0])
        self.assertEqual(set(first_rows[0]), set(FEATURE_NAMES))

    def test_synthetic_csv_contains_features_and_target_but_not_hidden_duration(self) -> None:
        output_path = Path(self.temp_directory.name) / "synthetic.csv"
        write_synthetic_csv(output_path, sample_count=30, seed=23)
        with output_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
        self.assertEqual(len(rows), 30)
        self.assertEqual(
            set(reader.fieldnames or ()),
            {*FEATURE_NAMES, "sla_breach"},
        )
        self.assertNotIn("simulated_resolution_hours", reader.fieldnames or ())
        self.assertTrue(
            set(row["sla_breach"] for row in rows).issubset({"0", "1"})
        )

    def test_training_and_reports_are_reproducible(self) -> None:
        second_path = Path(self.temp_directory.name) / "risk-model-second.joblib"
        second_json = Path(self.temp_directory.name) / "evaluation.json"
        second_markdown = Path(self.temp_directory.name) / "evaluation.md"
        second_report = train_and_evaluate(
            second_path,
            sample_count=1200,
            json_report_path=second_json,
            markdown_report_path=second_markdown,
        )
        for metric_name in (
            "baseline_prior_only_test",
            "random_forest_test",
            "hours_only_test",
            "threshold_selection",
            "split_counts",
        ):
            self.assertEqual(self.report[metric_name], second_report[metric_name])
        self.assertTrue(second_json.is_file())
        self.assertTrue(second_markdown.is_file())

    def test_synthetic_target_matches_simulated_resolution_definition(self) -> None:
        import random

        for seed in range(100):
            features, label, resolution_hours = simulate_synthetic_case(
                random.Random(seed)
            )
            self.assertEqual(
                label,
                int(resolution_hours > features["hours_remaining"]),
            )
            self.assertNotIn("sla_breach", features)
            self.assertNotIn("simulated_resolution_hours", features)

    def test_stratified_splits_have_no_shared_records(self) -> None:
        rows, labels = generate_synthetic_dataset(600, seed=19)
        train, validation, test, *_ = split_synthetic_dataset(rows, labels)
        identifiers = [{id(row) for row in part} for part in (train, validation, test)]
        self.assertEqual(len(train) + len(validation) + len(test), len(rows))
        self.assertFalse(identifiers[0] & identifiers[1])
        self.assertFalse(identifiers[0] & identifiers[2])
        self.assertFalse(identifiers[1] & identifiers[2])

    def test_split_seeds_produce_distinct_partitions(self) -> None:
        rows, labels = generate_synthetic_dataset(600, seed=71)
        first = split_synthetic_dataset(rows, labels, 10_042, 20_042)
        second = split_synthetic_dataset(rows, labels, 10_123, 20_123)
        first_test_ids = {id(row) for row in first[2]}
        second_test_ids = {id(row) for row in second[2]}
        self.assertNotEqual(first_test_ids, second_test_ids)

    def test_predictions_accept_unseen_payer_and_service_categories(self) -> None:
        features = {
            **self.features,
            "payer": "Unseen synthetic payer",
            "service": "Unseen synthetic service",
        }
        prediction = self.compatible_model.predict(features, "PA-TEST")
        self.assertGreaterEqual(prediction["breach_probability"], 0)
        self.assertLessEqual(prediction["breach_probability"], 1)

    def test_controlled_scenarios_return_probabilities_without_hard_rules(self) -> None:
        scenarios = (
            {
                **self.features,
                "hours_remaining": 220.0,
                "unresolved_document_count": 0,
                "workflow_failure_count": 0,
                "pending_task_count": 0,
            },
            {
                **self.features,
                "hours_remaining": 3.0,
                "unresolved_document_count": 4,
                "workflow_failure_count": 2,
                "pending_task_count": 3,
            },
            {
                **self.features,
                "case_status": "MORE INFORMATION REQUIRED",
            },
            {
                **self.features,
                "case_status": "SUBMITTED",
                "payer": "Unseen test payer",
                "service": "Unseen test service",
            },
        )
        outputs = [
            self.compatible_model.predict(features, f"PA-SCENARIO-{index}")
            for index, features in enumerate(scenarios)
        ]
        for output in outputs:
            self.assertGreaterEqual(output["breach_probability"], 0)
            self.assertLessEqual(output["breach_probability"], 1)
            self.assertIsInstance(output["predicted_sla_breach"], bool)
            self.assertEqual(output["model_version"], MODEL_VERSION)

    def test_prediction_rejects_invalid_status_and_missing_category(self) -> None:
        invalid = {**self.features, "case_status": "UNRECOGNIZED"}
        with self.assertRaisesRegex(InvalidRiskFeatures, "case_status"):
            self.compatible_model.predict(invalid, "PA-TEST")
        incomplete = dict(self.features)
        del incomplete["payer"]
        with self.assertRaisesRegex(InvalidRiskFeatures, "Missing required features"):
            self.compatible_model.predict(incomplete, "PA-TEST")


class SlaRiskApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        temporary_root = Path(self.temp_directory.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(database, "DB_PATH", temporary_root / "test.db"))

        def closing_connect():
            connection = sqlite3.connect(database.DB_PATH, factory=ClosingConnection)
            connection.row_factory = sqlite3.Row
            return connection

        self.stack.enter_context(patch.object(database, "connect", closing_connect))
        self.stack.enter_context(patch.object(main, "connect", closing_connect))
        model = SlaRiskModel(
            create_test_v2_artifact(temporary_root / "risk-model.joblib")
        )
        self.model = model
        self.stack.enter_context(patch.object(main, "predict_sla_breach_risk", model.predict))
        self.stack.enter_context(patch.object(main, "index_policies", lambda: {"indexed": 0}))
        self.client = self.stack.enter_context(TestClient(main.app))
        self.addCleanup(self.stack.close)

    def test_case_prediction_endpoint_uses_case_data(self) -> None:
        future_deadline = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        with database.connect() as db:
            db.execute(
                "UPDATE cases SET deadline=? WHERE id='PA-1042'",
                (future_deadline,),
            )
            db.execute(
                """INSERT INTO manual_follow_ups
                   (case_id,created_at,responsible_role,reason,status)
                   VALUES(?,?,?,?,?)""",
                (
                    "PA-1042",
                    datetime.now(timezone.utc).isoformat(),
                    "Test reviewer",
                    "Synthetic pending task",
                    "OPEN",
                ),
            )
        with patch.object(
            main, "predict_sla_breach_risk", wraps=self.model.predict
        ) as predictor:
            response = self.client.get("/api/cases/PA-1042/sla-risk-prediction")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["case_id"], "PA-1042")
        self.assertTrue(result["available"])
        self.assertFalse(result["reliable"])
        self.assertIsInstance(result["predicted_sla_breach"], bool)
        self.assertGreaterEqual(result["breach_probability"], 0)
        self.assertLessEqual(result["breach_probability"], 1)
        self.assertEqual(result["model_version"], "sla-breach-random-forest-v2")
        self.assertIn(result["risk_level"], {"LOW", "MEDIUM", "HIGH"})
        self.assertIn("factors", result)
        features = predictor.call_args.args[0]
        self.assertEqual(features["payer"], "Northstar Health")
        self.assertEqual(features["service"], "Lumbar MRI")
        self.assertEqual(features["case_status"], "PAYER PENDING")
        self.assertEqual(features["pending_task_count"], 1)
        self.assertEqual(features["unresolved_document_count"], 2)
        self.assertEqual(features["workflow_failure_count"], 1)

    def test_case_prediction_endpoint_rejects_already_passed_deadline(self) -> None:
        with database.connect() as db:
            db.execute(
                "UPDATE cases SET deadline=? WHERE id='PA-1042'",
                ("2020-01-01T00:00:00+00:00",),
            )
        response = self.client.get("/api/cases/PA-1042/sla-risk-prediction")
        self.assertEqual(response.status_code, 409)

    def test_case_prediction_endpoint_rejects_resolved_cases(self) -> None:
        with database.connect() as db:
            db.execute(
                "UPDATE cases SET status='APPROVED' WHERE id='PA-1042'"
            )
        response = self.client.get("/api/cases/PA-1042/sla-risk-prediction")
        self.assertEqual(response.status_code, 409)

    def test_case_prediction_endpoint_reports_missing_artifact(self) -> None:
        future_deadline = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        with database.connect() as db:
            db.execute(
                "UPDATE cases SET deadline=? WHERE id='PA-1042'",
                (future_deadline,),
            )
        with patch.object(
            main,
            "predict_sla_breach_risk",
            side_effect=ModelUnavailableError("SLA breach model artifact is missing"),
        ):
            response = self.client.get(
                "/api/cases/PA-1042/sla-risk-prediction"
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("artifact is missing", response.json()["detail"])

    def test_case_prediction_endpoint_returns_404_for_unknown_case(self) -> None:
        response = self.client.get("/api/cases/PA-DOES-NOT-EXIST/sla-risk-prediction")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
