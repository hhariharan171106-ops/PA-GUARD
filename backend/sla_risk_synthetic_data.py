"""Reproducible synthetic cases for development-only SLA-breach training."""

import argparse
import csv
import math
import random
from pathlib import Path
from typing import Any


CASE_STATUSES = (
    "NEW",
    "INTAKE REVIEW",
    "EVIDENCE REVIEW",
    "READY TO SUBMIT",
    "SUBMITTED",
    "PAYER PENDING",
    "MORE INFORMATION REQUIRED",
)

_PAYER_DELAYS = {
    "Northstar Health": 8.0,
    "Harbor Mutual": 12.0,
    "SummitCare": 18.0,
    "Cedar Valley Plan": 24.0,
}
_SERVICE_DELAYS = {
    "Lumbar MRI": 0.0,
    "Specialty infusion": 16.0,
    "Knee arthroscopy": 8.0,
    "Cardiac CT": 12.0,
    "Physical therapy": -4.0,
}
_STATUS_DELAYS = {
    "NEW": 8.0,
    "INTAKE REVIEW": 12.0,
    "EVIDENCE REVIEW": 18.0,
    "READY TO SUBMIT": 6.0,
    "SUBMITTED": 24.0,
    "PAYER PENDING": 36.0,
    "MORE INFORMATION REQUIRED": 48.0,
}
_PENDING_TASK_WEIGHTS = (0.38, 0.34, 0.18, 0.07, 0.03)
_UNRESOLVED_DOCUMENT_WEIGHTS = (0.20, 0.34, 0.27, 0.13, 0.06)
_WORKFLOW_FAILURE_WEIGHTS = (0.68, 0.22, 0.08, 0.02)


def simulate_synthetic_case(
    rng: random.Random,
) -> tuple[dict[str, Any], int, float]:
    """Simulate one case and return features, target, and hidden duration.

    The hidden duration exists for target validation only; it is never included
    in the feature mapping returned here or by generate_synthetic_dataset.
    """
    payers = tuple(_PAYER_DELAYS)
    services = tuple(_SERVICE_DELAYS)
    statuses = CASE_STATUSES
    status = rng.choices(
        statuses,
        weights=(0.12, 0.18, 0.22, 0.10, 0.12, 0.16, 0.10),
        k=1,
    )[0]
    payer = rng.choice(payers)
    service = rng.choice(services)
    hours_remaining = math.exp(rng.uniform(math.log(1.0), math.log(240.0)))
    unresolved_documents = rng.choices(
        range(len(_UNRESOLVED_DOCUMENT_WEIGHTS)),
        weights=_UNRESOLVED_DOCUMENT_WEIGHTS,
        k=1,
    )[0]
    workflow_failures = rng.choices(
        range(len(_WORKFLOW_FAILURE_WEIGHTS)),
        weights=_WORKFLOW_FAILURE_WEIGHTS,
        k=1,
    )[0]
    pending_tasks = rng.choices(
        range(len(_PENDING_TASK_WEIGHTS)),
        weights=_PENDING_TASK_WEIGHTS,
        k=1,
    )[0]

    typical_resolution_hours = (
        _STATUS_DELAYS[status]
        + _PAYER_DELAYS[payer]
        + _SERVICE_DELAYS[service]
        + 10.0 * unresolved_documents
        + 8.0 * workflow_failures
        + 7.0 * pending_tasks
    )
    simulated_resolution_hours = rng.lognormvariate(
        math.log(max(1.0, typical_resolution_hours)), 0.55
    )
    features = {
        "hours_remaining": hours_remaining,
        "payer": payer,
        "service": service,
        "case_status": status,
        "unresolved_document_count": unresolved_documents,
        "workflow_failure_count": workflow_failures,
        "pending_task_count": pending_tasks,
    }
    sla_breach = int(simulated_resolution_hours > hours_remaining)
    return features, sla_breach, simulated_resolution_hours


def generate_synthetic_dataset(
    sample_count: int = 6000, seed: int = 20261018
) -> tuple[list[dict[str, Any]], list[int]]:
    """Generate independent development rows; no real-world rates are asserted."""
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 20:
        raise ValueError("sample_count must be an integer of at least 20.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")

    rng = random.Random(seed)
    cases = [simulate_synthetic_case(rng) for _ in range(sample_count)]
    rows = [features for features, _, _ in cases]
    labels = [label for _, label, _ in cases]

    return rows, labels


def write_synthetic_csv(
    output_path: Path, sample_count: int = 6000, seed: int = 20261018
) -> Path:
    rows, labels = generate_synthetic_dataset(sample_count, seed)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [*rows[0], "sla_breach"]
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(
            {**row, "sla_breach": label} for row, label in zip(rows, labels, strict=True)
        )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "reports" / "sla_risk_synthetic_data.csv",
    )
    parser.add_argument("--samples", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=20261018)
    arguments = parser.parse_args()
    destination = write_synthetic_csv(
        arguments.output,
        sample_count=arguments.samples,
        seed=arguments.seed,
    )
    print(f"Wrote {arguments.samples} synthetic rows to {destination.resolve()}")


if __name__ == "__main__":
    main()
