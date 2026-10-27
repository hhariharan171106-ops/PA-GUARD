from __future__ import annotations

import math
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import NotFittedError, check_is_fitted

MODEL_PATH = Path(__file__).parent / "models" / "sla_breach_risk_v2.joblib"
MODEL_VERSION = "sla-breach-random-forest-v2"
BREACH_DECISION_THRESHOLD = 0.35
SYNTHETIC_DATA_NOTICE = (
    "Development-only estimate trained on synthetic simulated outcomes; not "
    "validated against representative real prior-authorization outcomes."
)

FEATURE_NAMES = (
    "hours_remaining",
    "payer",
    "service",
    "case_status",
    "unresolved_document_count",
    "workflow_failure_count",
    "pending_task_count",
)
CASE_STATUSES = (
    "NEW",
    "INTAKE REVIEW",
    "EVIDENCE REVIEW",
    "READY TO SUBMIT",
    "SUBMITTED",
    "PAYER PENDING",
    "MORE INFORMATION REQUIRED",
)


class InvalidRiskFeatures(ValueError):
    pass


class ModelUnavailableError(RuntimeError):
    """Raised when the explicitly trained model artifact is unavailable."""


def create_pipeline(random_state: int = 42) -> Pipeline:
    return Pipeline(
        [
            ("vectorizer", DictVectorizer()),
            (
                "classifier",
                RandomForestClassifier(
                    n_estimators=240,
                    min_samples_leaf=4,
                    max_features="sqrt",
                    random_state=random_state,
                    n_jobs=1,
                ),
            ),
        ]
    )


def _validate_features(features: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(features, dict):
        raise InvalidRiskFeatures("Features must be an object.")
    missing = [name for name in FEATURE_NAMES if name not in features]
    if missing:
        raise InvalidRiskFeatures(f"Missing required features: {', '.join(missing)}.")

    hours = features["hours_remaining"]
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours):
        raise InvalidRiskFeatures("hours_remaining must be a finite number.")
    if hours <= 0:
        raise InvalidRiskFeatures(
            "hours_remaining must be positive; an already-passed deadline is a deterministic SLA status."
        )

    status = features["case_status"]
    if not isinstance(status, str) or status not in CASE_STATUSES:
        raise InvalidRiskFeatures("case_status is not a supported case status.")

    validated: dict[str, Any] = {
        "hours_remaining": float(hours),
        "case_status": status,
    }
    for name in ("payer", "service"):
        value = features[name]
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise InvalidRiskFeatures(f"{name} must be a non-empty string of at most 100 characters.")
        validated[name] = value.strip()
    for name in (
        "unresolved_document_count",
        "workflow_failure_count",
        "pending_task_count",
    ):
        value = features[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidRiskFeatures(f"{name} must be a non-negative integer.")
        validated[name] = float(value)
    return validated


class SlaRiskModel:
    def __init__(self, model_path: Path = MODEL_PATH) -> None:
        self.model_path = Path(model_path)
        self._pipeline: Pipeline | None = None

    def _load(self) -> Pipeline:
        if self._pipeline is not None:
            return self._pipeline
        if not self.model_path.is_file():
            raise ModelUnavailableError(
                "SLA breach model artifact is missing. "
                "Run train_sla_risk_model.py before requesting predictions."
            )
        try:
            artifact = joblib.load(self.model_path)
        except (
            AttributeError,
            EOFError,
            ImportError,
            KeyError,
            OSError,
            pickle.UnpicklingError,
            TypeError,
            ValueError,
        ) as error:
            raise ModelUnavailableError(
                f"SLA breach model artifact could not be loaded: {error}"
            ) from error
        if not isinstance(artifact, dict) or artifact.get("model_version") != MODEL_VERSION:
            raise ModelUnavailableError("The saved SLA breach model has an unsupported format or version.")
        pipeline = artifact.get("pipeline")
        if not isinstance(pipeline, Pipeline) or list(pipeline.named_steps) != [
            "vectorizer",
            "classifier",
        ]:
            raise ModelUnavailableError("The saved SLA breach model failed validation.")
        classifier = pipeline.named_steps["classifier"]
        vectorizer = pipeline.named_steps["vectorizer"]
        if (
            not isinstance(classifier, RandomForestClassifier)
            or tuple(artifact.get("feature_names", ())) != FEATURE_NAMES
        ):
            raise ModelUnavailableError("The saved SLA breach model failed validation.")
        try:
            check_is_fitted(classifier)
            check_is_fitted(vectorizer)
        except NotFittedError as error:
            raise ModelUnavailableError(
                "The saved SLA breach model has not been fitted."
            ) from error
        if set(classifier.classes_) != {0, 1}:
            raise ModelUnavailableError("The saved SLA breach model failed validation.")
        self._pipeline = pipeline
        return self._pipeline

    def predict(self, features: dict[str, Any], case_id: str) -> dict[str, Any]:
        validated = _validate_features(features)
        if not isinstance(case_id, str) or not case_id.strip():
            raise InvalidRiskFeatures("case_id must be a non-empty string.")
        model = self._load()
        classifier = model.named_steps["classifier"]
        vectorizer = model.named_steps["vectorizer"]
        try:
            probabilities = model.predict_proba([validated])[0]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise ModelUnavailableError(
                "The saved SLA breach model could not produce a prediction."
            ) from error
        class_probabilities = dict(zip(classifier.classes_, probabilities, strict=True))
        breach_probability = float(class_probabilities[1])
        predicted_breach = breach_probability >= BREACH_DECISION_THRESHOLD
        risk_level = (
            "HIGH" if breach_probability >= 0.65
            else "MEDIUM" if predicted_breach
            else "LOW"
        )

        feature_names = vectorizer.get_feature_names_out()
        importances = classifier.feature_importances_
        field_importances = {name: 0.0 for name in FEATURE_NAMES}
        for name, importance in zip(feature_names, importances, strict=True):
            field = name.split("=", 1)[0]
            if field in field_importances:
                field_importances[field] += float(importance)
        descriptions = {
            "hours_remaining": f"{validated['hours_remaining']:.1f} hours remain until the recorded deadline.",
            "payer": f"Payer is {validated['payer']}.",
            "service": f"Requested service is {validated['service']}.",
            "case_status": f"Current case status is {validated['case_status']}.",
            "unresolved_document_count": (
                f"{int(validated['unresolved_document_count'])} document(s) are not verified."
            ),
            "workflow_failure_count": (
                f"{int(validated['workflow_failure_count'])} workflow failure(s) are recorded."
            ),
            "pending_task_count": (
                f"{int(validated['pending_task_count'])} pending follow-up task(s) are recorded."
            ),
        }
        factors = [
            {"factor": field, "description": descriptions[field], "importance": round(importance, 4)}
            for field, importance in sorted(
                field_importances.items(), key=lambda item: item[1], reverse=True
            )
            if importance > 0
        ][:4]
        explanation = (
            "Main model indicators (global feature importance, not causal explanations): "
            + "; ".join(factor["description"] for factor in factors)
        )
        return {
            "case_id": case_id,
            "available": True,
            "predicted_sla_breach": predicted_breach,
            "breach_probability": round(breach_probability, 4),
            "breach_decision_threshold": BREACH_DECISION_THRESHOLD,
            "model_version": MODEL_VERSION,
            "risk_level": risk_level,
            "risk_score": round(100 * breach_probability, 1),
            "score_meaning": (
                "Estimated probability that simulated resolution time exceeds the "
                "remaining SLA time; development-only synthetic model output."
            ),
            "factors": factors,
            "reliable": False,
            "reliability_note": SYNTHETIC_DATA_NOTICE,
            "explanation": explanation,
            "training_data_source": MODEL_VERSION,
            "evaluation_provenance": "Synthetic simulator artifacts only; no representative real-world outcomes were used.",
            "prediction_timestamp": datetime.now(timezone.utc).isoformat(),
            "decision_scope": "Advisory estimate only; not a clinical, authorization, or payer-submission decision.",
        }

_default_model = SlaRiskModel()


def predict_sla_breach_risk(features: dict[str, Any], case_id: str) -> dict[str, Any]:
    return _default_model.predict(features, case_id)
