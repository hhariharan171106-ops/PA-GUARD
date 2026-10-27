"""Reproducible evaluation experiment for medical document classification on the Eka dataset.

Dataset source: ekacare/medical_records_parsing_validation_set
This script:
1. Loads the dataset via Hugging Face `datasets`.
2. Inspects image availability, document types, dataset size, and MD5 duplicates.
3. Partitions the single upstream split into a stratified train/test split (seed=42).
4. Extracts lightweight visual layout features (aspect ratio, intensity statistics, edge/table gradients, projections, histogram).
5. Trains a lightweight classifier and compares against a majority-class baseline.
6. Computes precision, recall, F1-score, and confusion matrix.
7. Saves results as structured JSON and Markdown reports.
8. Explicitly documents domain limitations (does NOT overwrite production evidence-gap model).
"""

from collections import Counter
import json
from pathlib import Path
import time
import numpy as np
from PIL import Image
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from datasets import load_dataset


DATASET_NAME = "ekacare/medical_records_parsing_validation_set"
RANDOM_SEED = 42
REPORTS_DIR = Path(__file__).parent / "reports"


def extract_visual_features(image: Image.Image) -> list[float]:
    """Extract lightweight layout and intensity features from a PIL Image.
    
    Features:
    - aspect ratio (width / height)
    - mean grayscale intensity
    - intensity standard deviation
    - horizontal gradient mean (vertical edges / line density)
    - vertical gradient mean (horizontal edges / row structure)
    - horizontal row projection variance (line structure)
    - vertical column projection variance (column structure)
    - 8-bin grayscale intensity distribution
    """
    width, height = image.size
    aspect_ratio = float(width) / float(max(height, 1))

    # Resize to standard thumbnail (64x64) for lightweight, low-memory extraction
    gray = image.convert("L")
    thumb = gray.resize((64, 64), Image.Resampling.BILINEAR)
    arr = np.asarray(thumb, dtype=np.float32) / 255.0

    mean_val = float(np.mean(arr))
    std_val = float(np.std(arr))

    # Gradients represent table grid lines (lab reports) vs cursive/whitespace (prescriptions)
    grad_x = float(np.mean(np.abs(np.diff(arr, axis=1))))
    grad_y = float(np.mean(np.abs(np.diff(arr, axis=0))))

    # Row/column projection profiles capture tabular/columnar layout
    row_proj = np.mean(arr, axis=1)
    col_proj = np.mean(arr, axis=0)
    row_var = float(np.var(row_proj))
    col_var = float(np.var(col_proj))

    # 8-bin histogram for dark/light ink distribution
    hist, _ = np.histogram(arr, bins=8, range=(0.0, 1.0), density=True)
    hist_list = [float(val) for val in hist]

    return [aspect_ratio, mean_val, std_val, grad_x, grad_y, row_var, col_var] + hist_list


def run_experiment() -> dict:
    start_time = time.time()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Loading dataset '{DATASET_NAME}'...")
    ds = load_dataset(DATASET_NAME)
    split = ds["test"]
    total_samples = len(split)

    # 1. Dataset audit
    md5_list = split["md5_image"]
    unique_md5 = len(set(md5_list))
    duplicate_count = total_samples - unique_md5
    doc_types = split["document_type"]
    class_counts = dict(Counter(doc_types))

    print(f"Dataset loaded: {total_samples} samples across {unique_md5} unique images.")
    print(f"Document type distribution: {class_counts}")
    print(f"MD5 duplicates: {duplicate_count}")

    # 2. Extract features and verify images
    print("Extracting visual layout features from images...")
    features = []
    labels = []
    all_images_valid = True

    for idx, row in enumerate(split):
        img = row["image"]
        if not isinstance(img, Image.Image):
            all_images_valid = False
            continue
        feat = extract_visual_features(img)
        features.append(feat)
        labels.append(row["document_type"])

    X = np.array(features, dtype=np.float32)
    y = np.array(labels)

    # 3. Stratified train/validation split
    # Since upstream only provides a single 'test' split of 288 samples,
    # we create a reproducible 75% train / 25% test split.
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=RANDOM_SEED, stratify=y
    )

    classes = sorted(list(set(y)))
    train_dist = dict(Counter(y_train))
    test_dist = dict(Counter(y_test))

    print(f"Train set: {len(y_train)} samples {train_dist}")
    print(f"Test set:  {len(y_test)} samples {test_dist}")

    # 4. Majority-Class Baseline
    baseline = DummyClassifier(strategy="most_frequent")
    baseline.fit(X_train, y_train)
    y_pred_base = baseline.predict(X_test)

    baseline_metrics = {
        "accuracy": float(accuracy_score(y_test, y_pred_base)),
        "precision_macro": float(precision_score(y_test, y_pred_base, average="macro", zero_division=0)),
        "recall_macro": float(recall_score(y_test, y_pred_base, average="macro", zero_division=0)),
        "f1_macro": float(f1_score(y_test, y_pred_base, average="macro", zero_division=0)),
        "precision_weighted": float(precision_score(y_test, y_pred_base, average="weighted", zero_division=0)),
        "recall_weighted": float(recall_score(y_test, y_pred_base, average="weighted", zero_division=0)),
        "f1_weighted": float(f1_score(y_test, y_pred_base, average="weighted", zero_division=0)),
    }

    # 5. Lightweight Classifier (Logistic Regression with balanced class weights)
    clf = LogisticRegression(random_state=RANDOM_SEED, max_iter=1000, class_weight="balanced")
    clf.fit(X_train, y_train)
    y_pred = clf.predict(X_test)

    cm = confusion_matrix(y_test, y_pred, labels=classes).tolist()
    per_class_f1 = {
        cls: float(f1)
        for cls, f1 in zip(classes, f1_score(y_test, y_pred, labels=classes, average=None, zero_division=0))
    }
    per_class_precision = {
        cls: float(p)
        for cls, p in zip(classes, precision_score(y_test, y_pred, labels=classes, average=None, zero_division=0))
    }
    per_class_recall = {
        cls: float(r)
        for cls, r in zip(classes, recall_score(y_test, y_pred, labels=classes, average=None, zero_division=0))
    }

    model_metrics = {
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "precision_macro": float(precision_score(y_test, y_pred, average="macro", zero_division=0)),
        "recall_macro": float(recall_score(y_test, y_pred, average="macro", zero_division=0)),
        "f1_macro": float(f1_score(y_test, y_pred, average="macro", zero_division=0)),
        "precision_weighted": float(precision_score(y_test, y_pred, average="weighted", zero_division=0)),
        "recall_weighted": float(recall_score(y_test, y_pred, average="weighted", zero_division=0)),
        "f1_weighted": float(f1_score(y_test, y_pred, average="weighted", zero_division=0)),
        "confusion_matrix": cm,
        "classes": classes,
        "per_class": {
            cls: {
                "precision": per_class_precision[cls],
                "recall": per_class_recall[cls],
                "f1": per_class_f1[cls],
            }
            for cls in classes
        },
    }

    duration_sec = round(time.time() - start_time, 2)

    results = {
        "dataset_source": DATASET_NAME,
        "random_seed": RANDOM_SEED,
        "total_samples": total_samples,
        "unique_images": unique_md5,
        "duplicate_md5_count": duplicate_count,
        "all_images_valid": all_images_valid,
        "class_distribution": class_counts,
        "train_samples": len(y_train),
        "test_samples": len(y_test),
        "train_distribution": train_dist,
        "test_distribution": test_dist,
        "feature_count": X.shape[1],
        "feature_descriptions": [
            "aspect_ratio",
            "mean_intensity",
            "std_intensity",
            "grad_x_edge_density",
            "grad_y_row_density",
            "row_projection_variance",
            "col_projection_variance",
            "intensity_histogram_8bins",
        ],
        "majority_class_baseline": baseline_metrics,
        "lightweight_model": {
            "model_type": "LogisticRegression(class_weight='balanced')",
            "metrics": model_metrics,
        },
        "execution_time_seconds": duration_sec,
        "limitations": [
            "The Eka dataset contains only 288 samples across exactly two document types (Lab-report and Prescription).",
            "The dataset does not contain prior-authorization medical necessity labels or evidence sufficiency labels.",
            "It does not contain other essential prior-authorization document types (Clinical Notes, Imaging Reports, Treatment History, Specialist Consultation Letters).",
            "It does not contain extracted text features; sample_prompt and rubrics are generic parsing instructions rather than patient record text.",
            "Document format classification (identifying a document as a prescription or lab report) does NOT validate clinical necessity.",
            "The production evidence gap classifier in evidence_prediction_model.py is safely preserved and not overwritten.",
        ],
    }

    # Save JSON report
    json_path = REPORTS_DIR / "eka_document_classification_evaluation.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    # Save Markdown report
    md_path = REPORTS_DIR / "eka_document_classification_evaluation.md"
    md_content = f"""# Eka Medical Records Parsing Dataset Evaluation Report

## Experiment Summary
* **Dataset Source:** `{DATASET_NAME}`
* **Random Seed:** `{RANDOM_SEED}`
* **Total Samples:** {total_samples} (288 images)
* **Unique MD5s:** {unique_md5} (Duplicates: {duplicate_count})
* **Document Types:** {class_counts}
* **Split Scheme:** Stratified 75% Train ({len(y_train)}) / 25% Test ({len(y_test)})
* **Features Extracted:** {X.shape[1]} lightweight visual layout features (aspect ratio, line gradients, projection variance, 8-bin intensity histogram)
* **Execution Time:** {duration_sec}s

## Performance Comparison

| Metric | Majority Baseline (Lab-report) | Lightweight Classifier (Balanced LogReg) |
|---|---|---|
| Accuracy | {baseline_metrics['accuracy']:.4f} | {model_metrics['accuracy']:.4f} |
| Macro Precision | {baseline_metrics['precision_macro']:.4f} | {model_metrics['precision_macro']:.4f} |
| Macro Recall | {baseline_metrics['recall_macro']:.4f} | {model_metrics['recall_macro']:.4f} |
| Macro F1-Score | {baseline_metrics['f1_macro']:.4f} | {model_metrics['f1_macro']:.4f} |
| Weighted F1-Score | {baseline_metrics['f1_weighted']:.4f} | {model_metrics['f1_weighted']:.4f} |

### Per-Class Performance
* **Lab-report:** Precision = {per_class_precision.get('Lab-report', 0):.4f}, Recall = {per_class_recall.get('Lab-report', 0):.4f}, F1 = {per_class_f1.get('Lab-report', 0):.4f}
* **Prescription:** Precision = {per_class_precision.get('Prescription', 0):.4f}, Recall = {per_class_recall.get('Prescription', 0):.4f}, F1 = {per_class_f1.get('Prescription', 0):.4f}

### Confusion Matrix (Test Split: rows=Actual, cols=Predicted)
Classes: `{classes}`
```
{cm}
```

## Critical Domain Findings & Limitations
1. **Two-Class Restriction:** The Eka dataset only labels `Lab-report` and `Prescription`. Prior authorization requires diverse clinical documentation including Clinical Notes, Imaging Reports, Specialist Letters, and Treatment Histories.
2. **Format vs. Necessity:** Identifying that a document is a prescription or lab report establishes document type only; it **does not** establish whether payer prior-authorization clinical necessity criteria are satisfied.
3. **No Clinical Text Annotations:** The dataset provides prompt templates and evaluation rubrics for JSON parsing, not ground-truth patient clinical narratives.
4. **Safety & Compatibility:** The production TF-IDF evidence gap model (`evidence_prediction_model.py`) is preserved intact to prevent regression in the existing case review workflow.
"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"\nEvaluation complete in {duration_sec}s!")
    print(f"Results saved to:\n  {json_path}\n  {md_path}")
    print(f"Model Macro F1: {model_metrics['f1_macro']:.4f} vs Baseline: {baseline_metrics['f1_macro']:.4f}")

    return results


if __name__ == "__main__":
    run_experiment()
