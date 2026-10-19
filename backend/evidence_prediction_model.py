"""Experimental evidence-gap classifier using synthetic training examples only."""

from functools import lru_cache
import re
from typing import Mapping, Sequence

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline


class ModelUnavailableError(RuntimeError):
    """Raised when the experimental classifier cannot be initialized."""


_SYNTHETIC_EXAMPLES = (
    (
        "Clinical notes",
        "Clinical Notes",
        "Clinical notes describe symptoms, examination, and the treatment plan for review.",
        "NOT_PREDICTED_MISSING",
    ),
    (
        "Clinical notes",
        "Imaging Report",
        "Radiology images describe the scan findings and impression.",
        "PREDICTED_MISSING",
    ),
    (
        "Conservative treatment history",
        "Treatment History",
        "Conservative treatment history lists prior therapy, duration, and response.",
        "NOT_PREDICTED_MISSING",
    ),
    (
        "Conservative treatment history",
        "Clinical Notes",
        "Clinical notes describe the current examination and diagnosis.",
        "PREDICTED_MISSING",
    ),
    (
        "Lab results",
        "Laboratory Reports",
        "Lab results include the latest laboratory values and collection date.",
        "NOT_PREDICTED_MISSING",
    ),
    (
        "Lab results",
        "Prescription or Medication Records",
        "Medication records list the prescribed dose and refill history.",
        "PREDICTED_MISSING",
    ),
    (
        "Infusion order",
        "Other Supporting Evidence",
        "The infusion order specifies the requested medication and administration schedule.",
        "NOT_PREDICTED_MISSING",
    ),
    (
        "Infusion order",
        "Imaging Report",
        "The imaging report summarizes the radiology findings.",
        "PREDICTED_MISSING",
    ),
    (
        "Specialist letter",
        "Medical Necessity Documentation",
        "The specialist letter summarizes the consultation and recommended treatment.",
        "NOT_PREDICTED_MISSING",
    ),
    (
        "Specialist letter",
        "Laboratory Reports",
        "Laboratory reports contain test values and collection dates.",
        "PREDICTED_MISSING",
    ),
)

_PREDICTION_LABELS = {"NOT_PREDICTED_MISSING", "PREDICTED_MISSING"}


def _feature_text(
    required_document: str,
    document_text: str,
    categories: Sequence[str],
) -> str:
    return (
        f"Required evidence label: {required_document}\n"
        f"Document categories: {' '.join(categories)}\n"
        "Document processing completed: yes\n"
        f"Extracted document text: {document_text}"
    )


@lru_cache(maxsize=1)
def _get_model() -> Pipeline:
    training_texts = [
        _feature_text(label, text, [category])
        for label, category, text, _ in _SYNTHETIC_EXAMPLES
    ]
    training_labels = [target for *_, target in _SYNTHETIC_EXAMPLES]
    if set(training_labels) != _PREDICTION_LABELS:
        raise ModelUnavailableError("Synthetic examples do not cover both prediction classes.")

    model = Pipeline(
        [
            ("tfidf", TfidfVectorizer(ngram_range=(1, 2), lowercase=True)),
            ("classifier", LogisticRegression(random_state=0, max_iter=1000)),
        ]
    )
    try:
        model.fit(training_texts, training_labels)
    except ValueError as error:
        raise ModelUnavailableError(f"Experimental classifier could not be trained: {error}") from error
    return model


def _validated_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list of strings.")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must contain only strings.")
    return [item.strip() for item in value if item.strip()]


def _normalized_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def predict_evidence_gaps(
    required_documents: object,
    documents: object,
    evidence_finding: object,
) -> dict:
    """Predict possible evidence gaps without replacing deterministic verification."""
    required = _validated_string_list(required_documents, "required_documents")
    if not isinstance(documents, list) or any(not isinstance(item, Mapping) for item in documents):
        raise ValueError("documents must be a list of document records.")
    if not isinstance(evidence_finding, Mapping):
        raise ValueError("evidence_finding must be a finding record.")

    base = {
        "experimental": True,
        "validated": False,
        "training_data_source": "Synthetic examples only; no labelled real-world dataset.",
        "model_name": "TF-IDF + Logistic Regression",
    }
    if not required:
        return {
            **base,
            "available": False,
            "requirements_available": False,
            "message": "No applicable approved required-document labels are available.",
            "predictions": [],
        }

    usable_documents: list[dict] = []
    has_unreadable_document = bool(evidence_finding.get("unreadable_documents"))
    for document in documents:
        extracted_text = _normalized_text(document.get("extracted_text"))
        completed = document.get("ocr_status") == "COMPLETED"
        if not completed or len(re.sub(r"\s+", " ", extracted_text).strip()) < 20:
            if document.get("status") != "MISSING":
                has_unreadable_document = True
            continue
        usable_documents.append(
            {
                "text": extracted_text,
                "category": _normalized_text(document.get("category")),
            }
        )

    verification_unavailable = has_unreadable_document or not usable_documents
    if verification_unavailable:
        message = (
            "Document text is unavailable or unreadable; no evidence-absence prediction "
            "is made."
        )
    else:
        message = (
            "Experimental predictions use synthetic training examples and are not "
            "clinically or operationally validated."
        )

    found_labels = {
        item.get("required_document")
        for item in evidence_finding.get("found_documents", [])
        if isinstance(item, Mapping)
    }
    possible_labels = {
        item.get("required_document")
        for item in evidence_finding.get("possible_evidence_matches", [])
        if isinstance(item, Mapping)
    }
    missing_labels = set(evidence_finding.get("missing_documents", []))

    results = []
    model = None
    model_error = None
    if not verification_unavailable:
        try:
            model = _get_model()
        except ModelUnavailableError as error:
            model_error = str(error)

    for label in required:
        if verification_unavailable:
            verification_status = "UNKNOWN"
            prediction = "UNKNOWN"
        else:
            if label in found_labels:
                verification_status = "FOUND_IN_TEXT"
            elif label in possible_labels:
                verification_status = "NEEDS_REVIEW"
            elif label in missing_labels:
                verification_status = "VERIFIED_MISSING"
            else:
                verification_status = "NEEDS_REVIEW"

            if model is None:
                prediction = "UNKNOWN"
            else:
                feature = _feature_text(
                    label,
                    "\n".join(document["text"] for document in usable_documents),
                    [document["category"] for document in usable_documents if document["category"]],
                )
                prediction = str(model.predict([feature])[0])
                if prediction not in _PREDICTION_LABELS:
                    raise ModelUnavailableError("Experimental classifier returned an invalid prediction.")

        results.append(
            {
                "required_document": label,
                "ml_prediction": prediction,
                "verification_status": verification_status,
            }
        )

    return {
        **base,
        "available": model is not None and not verification_unavailable,
        "requirements_available": True,
        "message": (
            f"Experimental model unavailable: {model_error}"
            if model_error
            else message
        ),
        "predictions": results,
    }
