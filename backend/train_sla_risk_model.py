"""Train and evaluate an experimental SLA-breach model using synthetic cases."""

import argparse
import json
import re
from pathlib import Path
from typing import Any

import joblib
import sklearn
from sklearn.dummy import DummyClassifier
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split

from sla_risk_model import FEATURE_NAMES, create_pipeline
from sla_risk_synthetic_data import generate_synthetic_dataset

EXPERIMENTAL_MODEL_VERSION = "sla-breach-random-forest-v3-experimental"
DEFAULT_SEED = 20261018
SPLIT_SEED = 42
VALIDATION_SPLIT_SEED = 43
BREACH_RECALL_TARGET = 0.95
CALIBRATION_BINS = 10
BACKEND_DIR = Path(__file__).parent
DEFAULT_ARTIFACT_PATH = (
    BACKEND_DIR / "models" / "sla_breach_risk_v3_experimental.joblib"
)
DEFAULT_JSON_REPORT = BACKEND_DIR / "reports" / "sla_risk_evaluation.json"
DEFAULT_MARKDOWN_REPORT = BACKEND_DIR / "reports" / "sla_risk_evaluation.md"
TARGET_DESCRIPTION = (
    "sla_breach = 1 when simulated future resolution hours exceed positive "
    "hours remaining at prediction time; otherwise 0."
)
LIMITATIONS = [
    "Every payer, service, status weight, delay contribution, and duration "
    "distribution parameter is a transparent simulation assumption, not a "
    "verified healthcare statistic.",
    "Synthetic holdout performance measures only agreement with this simulator; "
    "it does not establish real-world predictive performance.",
    "The row-stratified split evaluates independent draws from the same "
    "simulator distribution, not temporal, payer-held-out, or distribution-shift "
    "generalization.",
    "Random Forest probabilities are not calibrated. Brier score and reliability "
    "bins are reported for diagnosis; no calibration transform is applied.",
]
GENERATOR_ASSUMPTIONS = {
    "hours_remaining_hours": (
        "Log-uniform between 1 and 240 hours; this is a simulation range, "
        "not a measured population distribution."
    ),
    "case_status_probabilities": {
        "NEW": 0.12,
        "INTAKE REVIEW": 0.18,
        "EVIDENCE REVIEW": 0.22,
        "READY TO SUBMIT": 0.10,
        "SUBMITTED": 0.12,
        "PAYER PENDING": 0.16,
        "MORE INFORMATION REQUIRED": 0.10,
    },
    "payer_delay_hours": {
        "Northstar Health": 8,
        "Harbor Mutual": 12,
        "SummitCare": 18,
        "Cedar Valley Plan": 24,
    },
    "service_delay_hours": {
        "Lumbar MRI": 0,
        "Specialty infusion": 16,
        "Knee arthroscopy": 8,
        "Cardiac CT": 12,
        "Physical therapy": -4,
    },
    "status_delay_hours": {
        "NEW": 8,
        "INTAKE REVIEW": 12,
        "EVIDENCE REVIEW": 18,
        "READY TO SUBMIT": 6,
        "SUBMITTED": 24,
        "PAYER PENDING": 36,
        "MORE INFORMATION REQUIRED": 48,
    },
    "unresolved_document_count_probabilities": [0.20, 0.34, 0.27, 0.13, 0.06],
    "workflow_failure_count_probabilities": [0.68, 0.22, 0.08, 0.02],
    "pending_task_count_probabilities": [0.38, 0.34, 0.18, 0.07, 0.03],
    "resolution_median_hours": (
        "max(1, status_delay + payer_delay + service_delay + "
        "10*unresolved_documents + 8*workflow_failures + 7*pending_tasks)"
    ),
    "resolution_duration_noise": "Log-normal with sigma=0.55",
    "target": TARGET_DESCRIPTION,
    "feature_exclusions": [
        "simulated_resolution_hours",
        "sla_breach",
        "patient identifiers",
        "document text",
    ],
}


def _class_distribution(labels: list[int]) -> dict[str, Any]:
    count = len(labels)
    positives = sum(labels)
    negatives = count - positives
    return {
        "sample_count": count,
        "no_breach": {
            "count": negatives,
            "proportion": round(negatives / count, 6) if count else None,
        },
        "breach": {
            "count": positives,
            "proportion": round(positives / count, 6) if count else None,
        },
    }


def _predict_at_threshold(
    probabilities: list[float], threshold: float
) -> list[int]:
    return [int(probability >= threshold) for probability in probabilities]


def _select_recall_threshold(
    labels: list[int],
    probabilities: list[float],
    recall_target: float = BREACH_RECALL_TARGET,
) -> dict[str, Any]:
    if not 0 < recall_target <= 1:
        raise ValueError("recall_target must be in the interval (0, 1].")
    if not labels or len(labels) != len(probabilities) or 1 not in labels:
        raise ValueError("Threshold selection requires matching validation rows with breaches.")

    candidates = sorted({0.0, *probabilities})
    eligible: list[tuple[float, float, float, float]] = []
    for threshold in candidates:
        predictions = _predict_at_threshold(probabilities, threshold)
        recall = float(recall_score(labels, predictions, zero_division=0))
        if recall >= recall_target:
            precision = float(precision_score(labels, predictions, zero_division=0))
            eligible.append(
                (
                    precision,
                    threshold,
                    recall,
                    float(f1_score(labels, predictions, zero_division=0)),
                )
            )
    if not eligible:
        return {
            "threshold": 0.0,
            "recall_target": recall_target,
            "recall_target_met": False,
            "selection_rule": "No validation threshold met the requested breach recall.",
        }

    precision, threshold, recall, f1 = max(
        eligible, key=lambda item: (item[0], item[1])
    )
    return {
        "threshold": round(float(threshold), 8),
        "recall_target": recall_target,
        "recall_target_met": recall >= recall_target,
        "validation_precision_breach": round(precision, 6),
        "validation_recall_breach": round(recall, 6),
        "validation_f1_breach": round(f1, 6),
        "selection_rule": (
            "Among thresholds meeting the recall target, maximize breach "
            "precision; break ties by choosing the highest threshold."
        ),
    }


def _calibration_bins(
    labels: list[int], probabilities: list[float]
) -> list[dict[str, float | int]]:
    buckets: list[list[tuple[int, float]]] = [
        [] for _ in range(CALIBRATION_BINS)
    ]
    for label, probability in zip(labels, probabilities, strict=True):
        index = min(int(probability * CALIBRATION_BINS), CALIBRATION_BINS - 1)
        buckets[index].append((label, probability))
    return [
        {
            "bin": index,
            "sample_count": len(bucket),
            "mean_predicted_probability": round(
                sum(probability for _, probability in bucket) / len(bucket), 6
            ),
            "observed_breach_rate": round(
                sum(label for label, _ in bucket) / len(bucket), 6
            ),
        }
        for index, bucket in enumerate(buckets)
        if bucket
    ]


def _evaluate(
    labels: list[int], probabilities: list[float], threshold: float
) -> dict[str, Any]:
    predicted = _predict_at_threshold(probabilities, threshold)
    matrix = confusion_matrix(labels, predicted, labels=[0, 1]).tolist()
    true_negative, false_positive = matrix[0]
    false_negative, true_positive = matrix[1]
    metrics: dict[str, Any] = {
        "decision_threshold": round(float(threshold), 8),
        "accuracy": round(float(accuracy_score(labels, predicted)), 6),
        "precision_breach": round(
            float(precision_score(labels, predicted, zero_division=0)), 6
        ),
        "recall_breach": round(float(recall_score(labels, predicted, zero_division=0)), 6),
        "f1_breach": round(float(f1_score(labels, predicted, zero_division=0)), 6),
        "confusion_matrix_labels_0_1": matrix,
        "true_negative": true_negative,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_positive": true_positive,
        "false_negative_rate": round(
            false_negative / (false_negative + true_positive), 6
        )
        if false_negative + true_positive
        else None,
        "classification_report": classification_report(
            labels,
            predicted,
            labels=[0, 1],
            target_names=["no_breach", "breach"],
            output_dict=True,
            zero_division=0,
        ),
        "roc_auc": round(float(roc_auc_score(labels, probabilities)), 6)
        if len(set(labels)) == 2
        else None,
        "pr_auc_average_precision": round(
            float(average_precision_score(labels, probabilities)), 6
        )
        if len(set(labels)) == 2
        else None,
        "brier_score": round(float(brier_score_loss(labels, probabilities)), 6),
        "calibration_bins": _calibration_bins(labels, probabilities),
    }
    return metrics


def split_synthetic_dataset(
    rows: list[dict[str, Any]],
    labels: list[int],
    split_seed: int = SPLIT_SEED,
    validation_split_seed: int = VALIDATION_SPLIT_SEED,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[int],
    list[int],
    list[int],
]:
    if len(rows) != len(labels) or len(set(labels)) != 2:
        raise ValueError("A stratified split requires matching rows and both classes.")
    development_rows, test_rows, development_labels, test_labels = train_test_split(
        rows,
        labels,
        test_size=0.20,
        random_state=split_seed,
        stratify=labels,
    )
    train_rows, validation_rows, train_labels, validation_labels = train_test_split(
        development_rows,
        development_labels,
        test_size=0.25,
        random_state=validation_split_seed,
        stratify=development_labels,
    )
    return (
        train_rows,
        validation_rows,
        test_rows,
        train_labels,
        validation_labels,
        test_labels,
    )


def _markdown_report(report: dict[str, Any]) -> str:
    baseline = report["baseline_prior_only_test"]
    forest = report["random_forest_test"]
    threshold = report["threshold_selection"]
    return f"""# SLA breach-risk evaluation (synthetic development data)

## Executive summary

The model was evaluated against a sealed stratified test set generated by the
documented simulator. These metrics show how well it recovers that simulator's
outcomes; they are **not evidence of real-world healthcare performance** and
must not be used to claim clinical or operational validation.

The breach target is: `{report['target_definition']}`

## Dataset and split

- Source: {report['dataset']['source']}
- Generator seed: {report['dataset']['seed']}
- Split seeds: {report['split_seeds']}
- Total samples: {report['dataset']['class_distribution']['sample_count']}
- Training / validation / test: {report['split_counts']['train']} / {report['split_counts']['validation']} / {report['split_counts']['test']}
- Class counts (no breach / breach): {report['dataset']['class_distribution']['no_breach']['count']} / {report['dataset']['class_distribution']['breach']['count']}
- Features: {', '.join(report['dataset']['features'])}

Each record is independently sampled with stochastic resolution time. A
row-stratified split therefore tests interpolation within the same simulator,
not time-based or payer-held-out generalization. No patient data is used.

## Threshold selection

The test set did not participate in threshold selection. On validation data, a
threshold of **{threshold['threshold']:.4f}** was selected by maximizing breach
precision among thresholds that meet the predeclared breach recall target of
{threshold['recall_target']:.2f}. Validation target met:
{threshold['recall_target_met']}.

## Sealed test results

| Metric | Prior-only baseline | Random Forest |
|---|---:|---:|
| Accuracy | {baseline['accuracy']:.4f} | {forest['accuracy']:.4f} |
| Breach precision | {baseline['precision_breach']:.4f} | {forest['precision_breach']:.4f} |
| Breach recall | {baseline['recall_breach']:.4f} | {forest['recall_breach']:.4f} |
| Breach F1 | {baseline['f1_breach']:.4f} | {forest['f1_breach']:.4f} |
| ROC-AUC | {baseline['roc_auc']:.4f} | {forest['roc_auc']:.4f} |
| PR-AUC (average precision) | {baseline['pr_auc_average_precision']:.4f} | {forest['pr_auc_average_precision']:.4f} |
| Brier score (lower is better) | {baseline['brier_score']:.4f} | {forest['brier_score']:.4f} |

Random Forest confusion matrix (rows actual [no breach, breach], columns
predicted [no breach, breach]): `{forest['confusion_matrix_labels_0_1']}`

- False positives: {forest['false_positive']}
- False negatives: {forest['false_negative']}
- Breach false-negative rate: {forest['false_negative_rate']:.4f}

Accuracy alone hides the cost of missed breaches and can look acceptable when
one class dominates. Review breach recall, precision, false negatives, and
threshold-dependent tradeoffs together.

## Shortcut and calibration diagnostics

- Hours-remaining-only diagnostic ROC-AUC: {report['hours_only_test']['roc_auc']:.4f}
- Hours-remaining-only diagnostic PR-AUC: {report['hours_only_test']['pr_auc_average_precision']:.4f}
- Random Forest test Brier score: {forest['brier_score']:.4f}
- Calibration bins: see `random_forest_test.calibration_bins` in the JSON report.
- Random Forest global feature importance: {', '.join(f"{item['feature']}={item['importance']:.3f}" for item in report['random_forest_global_feature_importance'][:5])}

Hours remaining is expected to be a strong feature because it is directly
compared with simulated resolution duration to construct the breach target.
The hours-only model is included to make this shortcut explicit; its score is
not evidence that the simulator captures the full operational problem.
Global feature importance is a model-level summary, **not a causal or
case-specific explanation**. Calibration is assessed but not applied.

## Synthetic assumptions and limitations

{chr(10).join(f'- {item}' for item in report['limitations'])}

No payer, service, case-status delay, workload effect, task count distribution,
or timing distribution is a verified real-world statistic. These assumptions
are only a reproducible development scenario. The experimental artifact is not
the live API model and is not selected for production.

## Recommended next steps

1. Collect time-stamped, appropriately de-identified historical features as
   they existed at prediction time, plus verified SLA breach outcomes.
2. Define cohort eligibility and label windows with operations/compliance;
   exclude outcomes not observable at the prediction timestamp.
3. Use chronological evaluation, group-aware checks, representative external
   validation, subgroup/error analysis, and probability calibration on training
   or validation data only.
4. Select alert thresholds with SLA owners based on the operational cost of
   false negatives and false positives.
5. Keep hard deadlines, mandatory escalation rules, and human review in the
   deterministic workflow, independent from model scores.
"""


def train_and_evaluate(
    output_path: Path = DEFAULT_ARTIFACT_PATH,
    sample_count: int = 6000,
    seed: int = DEFAULT_SEED,
    json_report_path: Path | None = DEFAULT_JSON_REPORT,
    markdown_report_path: Path | None = DEFAULT_MARKDOWN_REPORT,
    split_seed: int = SPLIT_SEED,
    validation_split_seed: int = VALIDATION_SPLIT_SEED,
) -> dict[str, Any]:
    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing model artifact: {destination}. "
            "Choose a new --output path so the existing model remains recoverable."
        )
    rows, labels = generate_synthetic_dataset(sample_count=sample_count, seed=seed)
    (
        train_rows,
        validation_rows,
        test_rows,
        train_labels,
        validation_labels,
        test_labels,
    ) = split_synthetic_dataset(rows, labels)

    forest = create_pipeline(random_state=seed)
    baseline = DummyClassifier(strategy="prior")
    forest.fit(train_rows, train_labels)
    baseline.fit(train_rows, train_labels)
    validation_probabilities = forest.predict_proba(validation_rows)[:, 1].tolist()
    selected_threshold = _select_recall_threshold(
        validation_labels, validation_probabilities
    )
    threshold = selected_threshold["threshold"]

    baseline_validation_probabilities = baseline.predict_proba(validation_rows)[
        :, 1
    ].tolist()
    baseline_threshold_selection = _select_recall_threshold(
        validation_labels, baseline_validation_probabilities
    )
    test_probabilities = forest.predict_proba(test_rows)[:, 1].tolist()
    baseline_probabilities = baseline.predict_proba(test_rows)[:, 1].tolist()
    hours_only = create_pipeline(random_state=seed)
    hours_only.fit(
        [{"hours_remaining": row["hours_remaining"]} for row in train_rows],
        train_labels,
    )
    hours_only_validation_probabilities = hours_only.predict_proba(
        [{"hours_remaining": row["hours_remaining"]} for row in validation_rows]
    )[:, 1].tolist()
    hours_only_threshold_selection = _select_recall_threshold(
        validation_labels, hours_only_validation_probabilities
    )
    hours_only_probabilities = hours_only.predict_proba(
        [{"hours_remaining": row["hours_remaining"]} for row in test_rows]
    )[:, 1].tolist()
    class_names = forest.named_steps["classifier"].classes_.tolist()
    if class_names != [0, 1]:
        raise RuntimeError(f"Unexpected model classes: {class_names}")

    class_importances = forest.named_steps["classifier"].feature_importances_
    feature_names = forest.named_steps["vectorizer"].get_feature_names_out()
    field_importances = {feature: 0.0 for feature in FEATURE_NAMES}
    for feature_name, importance in zip(feature_names, class_importances, strict=True):
        feature = feature_name.split("=", 1)[0]
        if feature in field_importances:
            field_importances[feature] += float(importance)
    ordered_importances = sorted(
        (
            {"feature": feature, "importance": round(importance, 6)}
            for feature, importance in field_importances.items()
        ),
        key=lambda item: item["importance"],
        reverse=True,
    )
    full_model_metrics = _evaluate(test_labels, test_probabilities, threshold)
    feature_ablation_metrics: dict[str, Any] = {}
    for excluded_feature in (
        "payer",
        "service",
        "case_status",
        "unresolved_document_count",
        "pending_task_count",
        "workflow_failure_count",
    ):
        ablated_train = [
            {key: value for key, value in row.items() if key != excluded_feature}
            for row in train_rows
        ]
        ablated_validation = [
            {key: value for key, value in row.items() if key != excluded_feature}
            for row in validation_rows
        ]
        ablated_test = [
            {key: value for key, value in row.items() if key != excluded_feature}
            for row in test_rows
        ]
        ablated_model = create_pipeline(random_state=seed)
        ablated_model.fit(ablated_train, train_labels)
        ablated_validation_probabilities = ablated_model.predict_proba(
            ablated_validation
        )[:, 1].tolist()
        ablated_threshold = _select_recall_threshold(
            validation_labels, ablated_validation_probabilities
        )
        ablated_test_probabilities = ablated_model.predict_proba(
            ablated_test
        )[:, 1].tolist()
        ablated_metrics = _evaluate(
            test_labels,
            ablated_test_probabilities,
            ablated_threshold["threshold"],
        )
        feature_ablation_metrics[excluded_feature] = {
            "excluded_feature": excluded_feature,
            "threshold_selection": ablated_threshold,
            "test": ablated_metrics,
            "change_when_feature_is_removed": {
                "roc_auc": round(
                    ablated_metrics["roc_auc"] - full_model_metrics["roc_auc"], 6
                ),
                "pr_auc_average_precision": round(
                    ablated_metrics["pr_auc_average_precision"]
                    - full_model_metrics["pr_auc_average_precision"],
                    6,
                ),
                "brier_score": round(
                    ablated_metrics["brier_score"] - full_model_metrics["brier_score"],
                    6,
                ),
            },
        }

    report: dict[str, Any] = {
        "model_version": EXPERIMENTAL_MODEL_VERSION,
        "target_definition": TARGET_DESCRIPTION,
        "dataset": {
            "source": "synthetic future-resolution simulation; not real case outcomes",
            "seed": seed,
            "sample_count": len(rows),
            "features": list(FEATURE_NAMES),
            "generator_assumptions": GENERATOR_ASSUMPTIONS,
            "class_distribution": _class_distribution(labels),
            "split_class_distribution": {
                "train": _class_distribution(train_labels),
                "validation": _class_distribution(validation_labels),
                "test": _class_distribution(test_labels),
            },
            "records_are_independent_draws": True,
            "duplicate_template_leakage": (
                "No template IDs or repeated base scenarios are used. Records are "
                "independent RNG draws; naturally recurring categories are expected."
            ),
            "exact_duplicate_feature_rows": len(
                rows
            )
            - len({json.dumps(row, sort_keys=True) for row in rows}),
        },
        "evaluation_protocol": {
            "split": "stratified 60% train / 20% validation / 20% sealed test",
            "test_used_for_threshold_selection": False,
            "preprocessing_fit_partition": "train only",
            "threshold_selection_partition": "validation only",
            "threshold_selection_objective": (
                "maximize breach precision subject to validation breach recall >= "
                f"{BREACH_RECALL_TARGET:.2f}"
            ),
            "scikit_learn_version": sklearn.__version__,
            "random_forest_parameters": {
                "n_estimators": 240,
                "min_samples_leaf": 4,
                "max_features": "sqrt",
                "random_state": seed,
                "n_jobs": 1,
            },
            "feature_ablation": (
                "Each listed feature is removed in turn; its model is fitted on "
                "training rows, threshold-selected on validation rows, and "
                "evaluated once on the sealed test rows. This measures incremental "
                "predictive value within this simulator, not causality."
            ),
        },
        "generator_assumptions": GENERATOR_ASSUMPTIONS,
        "split_seeds": {
            "development_vs_test": split_seed,
            "train_vs_validation": validation_split_seed,
        },
        "split_counts": {
            "train": len(train_rows),
            "validation": len(validation_rows),
            "test": len(test_rows),
        },
        "threshold_selection": selected_threshold,
        "baseline_prior_only_threshold_selection": baseline_threshold_selection,
        "hours_only_threshold_selection": hours_only_threshold_selection,
        "baseline_prior_only_test": _evaluate(
            test_labels,
            baseline_probabilities,
            baseline_threshold_selection["threshold"],
        ),
        "random_forest_test": full_model_metrics,
        "hours_only_test": _evaluate(
            test_labels,
            hours_only_probabilities,
            hours_only_threshold_selection["threshold"],
        ),
        "feature_ablation_test": feature_ablation_metrics,
        "random_forest_global_feature_importance": ordered_importances,
        "limitations": LIMITATIONS,
    }

    artifact = {
        "model_version": EXPERIMENTAL_MODEL_VERSION,
        "feature_names": FEATURE_NAMES,
        "training_data_source": report["dataset"]["source"],
        "seed": seed,
        "split_seeds": report["split_seeds"],
        "threshold": threshold,
        "threshold_selection": selected_threshold,
        "pipeline": forest,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, destination)
    report["artifact_path"] = str(destination.resolve())

    if json_report_path is not None:
        json_path = Path(json_report_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        report["json_report_path"] = str(json_path.resolve())
    if markdown_report_path is not None:
        markdown_path = Path(markdown_report_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        report["markdown_report_path"] = str(markdown_path.resolve())
    if json_report_path is not None:
        json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if markdown_report_path is not None:
        markdown_path.write_text(_markdown_report(report), encoding="utf-8")
    return report


def _multi_seed_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Multi-seed SLA-breach evaluation (synthetic data)",
        "",
        "Every result below is a synthetic-data evaluation against the stated "
        "simulator, not real-world healthcare performance.",
        "",
        f"- Samples per independent run: {report['sample_count']}",
        f"- Seeds: {', '.join(str(seed) for seed in report['seeds'])}",
        "- Split per run: stratified 60% train / 20% validation / 20% test.",
        "- Each model selects its own threshold from that run's validation split only.",
        "",
        "## Test-set metrics by seed and model",
        "",
        "| Seed | Model | Threshold | Precision | Recall | F1 | ROC-AUC | PR-AUC | Brier | Confusion matrix | FN |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for run in report["runs"]:
        for key, label in (
            ("random_forest_test", "Random Forest"),
            ("baseline_prior_only_test", "Prior-only"),
            ("hours_only_test", "Hours only"),
        ):
            metrics = run[key]
            matrix = json.dumps(metrics["confusion_matrix_labels_0_1"])
            lines.append(
                f"| {run['seed']} | {label} | {metrics['decision_threshold']:.4f} "
                f"| {metrics['precision_breach']:.4f} "
                f"| {metrics['recall_breach']:.4f} | {metrics['f1_breach']:.4f} "
                f"| {metrics['roc_auc']:.4f} | "
                f"{metrics['pr_auc_average_precision']:.4f} "
                f"| {metrics['brier_score']:.4f} | `{matrix}` "
                f"| {metrics['false_negative']} |"
            )

    lines.extend(
        [
            "",
            "## Feature ablation",
            "",
            "Each result is the test-metric change after removing one feature "
            "from the full model. Negative AUC/PR-AUC changes indicate worse "
            "ranking without that feature; positive Brier changes indicate "
            "worse probability error. These are simulator-specific incremental "
            "comparisons, not causal effects.",
            "",
            "| Seed | Removed feature | Δ ROC-AUC | Δ PR-AUC | Δ Brier | FN after removal |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for run in report["runs"]:
        for feature, ablation in run["feature_ablation_test"].items():
            changes = ablation["change_when_feature_is_removed"]
            lines.append(
                f"| {run['seed']} | {feature} | {changes['roc_auc']:+.4f} "
                f"| {changes['pr_auc_average_precision']:+.4f} "
                f"| {changes['brier_score']:+.4f} "
                f"| {ablation['test']['false_negative']} |"
            )

    lines.extend(
        [
            "",
            "## Across-seed Random Forest spread",
            "",
            "| Metric | Minimum | Maximum | Range |",
            "|---|---:|---:|---:|",
        ]
    )
    for metric in (
        "precision_breach",
        "recall_breach",
        "f1_breach",
        "roc_auc",
        "pr_auc_average_precision",
        "brier_score",
    ):
        spread = report["random_forest_metric_spread"][metric]
        lines.append(
            f"| {metric} | {spread['min']:.4f} | {spread['max']:.4f} "
            f"| {spread['range']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## Leakage and target definition",
            "",
            report["target_definition"],
            "",
            "At generation time, synthetic payer, service, status, unresolved "
            "document count, workflow failure count, and pending task count "
            "contribute to a typical simulated resolution duration. Stochastic "
            "log-normal noise is applied. A breach label is 1 when that hidden "
            "duration exceeds positive `hours_remaining`; otherwise it is 0. "
            "The hidden duration and label are not model features.",
            "",
            "Rows are independently sampled; exact repeated feature rows are "
            "counted in each per-seed report. Train, validation, and test "
            "partitions are disjoint; preprocessing/model fitting uses train "
            "only, threshold selection uses validation only, and the test set "
            "is used for reporting only. Shared simulator rules across splits "
            "are intentional and mean this does not test temporal or "
            "distribution-shift generalization.",
            "",
            "## Conclusion and limitations",
            "",
            "Seed-to-seed stability here supports only more offline testing "
            "against this simulator. It does not establish deployment "
            "readiness. Real-world assessment requires time-stamped, "
            "representative and appropriately de-identified historical cases, "
            "verified SLA breach outcomes, temporal and payer/service-group "
            "holdouts, calibration, error/subgroup analysis, operational "
            "threshold review, and prospective shadow-mode evaluation. The "
            "model must not make clinical, authorization, or payer-submission "
            "decisions.",
            "",
        ]
    )
    return "\n".join(lines)


def run_seed_experiments(
    seeds: list[int],
    sample_count: int = 6000,
    output_directory: Path = BACKEND_DIR / "models",
    report_directory: Path = BACKEND_DIR / "reports",
    run_id: str | None = None,
) -> dict[str, Any]:
    if not seeds or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds):
        raise ValueError("seeds must be a non-empty list of integers.")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique.")
    if any(seed < 0 or seed > 2**32 - 1 - 20_000 for seed in seeds):
        raise ValueError("seeds must be between 0 and 2**32 - 1 - 20,000.")
    if run_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("run_id may contain only letters, digits, hyphens, and underscores.")

    model_directory = Path(output_directory)
    reports = Path(report_directory)
    seed_label = "_".join(str(seed) for seed in seeds)
    suffix = f"_{run_id}" if run_id else ""
    aggregate_json = reports / f"sla_risk_multiseed_{seed_label}{suffix}.json"
    aggregate_markdown = reports / f"sla_risk_multiseed_{seed_label}{suffix}.md"
    paths = [aggregate_json, aggregate_markdown]
    for seed in seeds:
        paths.extend(
            [
                model_directory
                / f"sla_breach_risk_v3_experimental_seed_{seed}{suffix}.joblib",
                reports / f"sla_risk_evaluation_seed_{seed}{suffix}.json",
                reports / f"sla_risk_evaluation_seed_{seed}{suffix}.md",
            ]
        )
    existing_paths = [str(path) for path in paths if path.exists()]
    if existing_paths:
        raise FileExistsError(
            "Refusing to overwrite existing experiment outputs: "
            + ", ".join(existing_paths)
        )

    runs: list[dict[str, Any]] = []
    for seed in seeds:
        result = train_and_evaluate(
            output_path=(
                model_directory
                / f"sla_breach_risk_v3_experimental_seed_{seed}{suffix}.joblib"
            ),
            sample_count=sample_count,
            seed=seed,
            json_report_path=reports / f"sla_risk_evaluation_seed_{seed}{suffix}.json",
            markdown_report_path=reports / f"sla_risk_evaluation_seed_{seed}{suffix}.md",
            split_seed=seed + 10_000,
            validation_split_seed=seed + 20_000,
        )
        runs.append(
            {
                "seed": seed,
                "split_seeds": result["split_seeds"],
                "split_counts": result["split_counts"],
                "class_distribution": result["dataset"]["class_distribution"],
                "exact_duplicate_feature_rows": result["dataset"][
                    "exact_duplicate_feature_rows"
                ],
                "threshold_selection": result["threshold_selection"],
                "baseline_prior_only_threshold_selection": result[
                    "baseline_prior_only_threshold_selection"
                ],
                "hours_only_threshold_selection": result[
                    "hours_only_threshold_selection"
                ],
                "random_forest_test": result["random_forest_test"],
                "baseline_prior_only_test": result["baseline_prior_only_test"],
                "hours_only_test": result["hours_only_test"],
                "feature_ablation_test": result["feature_ablation_test"],
                "artifact_path": result["artifact_path"],
                "json_report_path": result["json_report_path"],
                "markdown_report_path": result["markdown_report_path"],
            }
        )

    metric_names = (
        "precision_breach",
        "recall_breach",
        "f1_breach",
        "roc_auc",
        "pr_auc_average_precision",
        "brier_score",
    )
    metric_spread = {}
    for metric in metric_names:
        values = [run["random_forest_test"][metric] for run in runs]
        metric_spread[metric] = {
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "range": round(max(values) - min(values), 6),
        }
    aggregate: dict[str, Any] = {
        "source": "synthetic simulator; not real case outcomes",
        "target_definition": TARGET_DESCRIPTION,
        "sample_count": sample_count,
        "seeds": seeds,
        "run_id": run_id,
        "split_protocol": "stratified 60% train / 20% validation / 20% test per seed",
        "random_forest_metric_spread": metric_spread,
        "runs": runs,
        "model_decision_safety": (
            "Offline experimental artifacts only. Do not load into the live API "
            "or use for clinical, authorization, or payer-submission decisions."
        ),
        "aggregate_json_path": str(aggregate_json.resolve()),
        "aggregate_markdown_path": str(aggregate_markdown.resolve()),
    }
    model_directory.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    aggregate_json.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    aggregate_markdown.write_text(_multi_seed_markdown(aggregate), encoding="utf-8")
    return aggregate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        help="Run independent synthetic experiments with unique artifacts per seed.",
    )
    parser.add_argument(
        "--run-id",
        help="Optional unique filename suffix for multi-seed output files.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT_PATH)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT)
    arguments = parser.parse_args()
    if arguments.seeds:
        result = run_seed_experiments(
            arguments.seeds,
            sample_count=arguments.samples,
            run_id=arguments.run_id,
        )
        print(json.dumps(result, indent=2))
        return
    report = train_and_evaluate(
        output_path=arguments.output,
        sample_count=arguments.samples,
        seed=arguments.seed,
        json_report_path=arguments.json_report,
        markdown_report_path=arguments.markdown_report,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
