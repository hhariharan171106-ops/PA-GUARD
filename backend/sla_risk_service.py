import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import HTTPException

from case_deadline import InvalidCaseDeadline, parse_case_deadline
from database import connect
from sla_risk_model import (
    InvalidRiskFeatures,
    ModelUnavailableError,
    predict_sla_breach_risk,
)


def predict_case_sla_risk(
    case_id: str,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    prediction_function: Callable[[dict, str], dict] | None = None,
) -> dict:
    with (connection_factory or connect)() as db:
        case = db.execute(
            "SELECT id, deadline, status, payer, service FROM cases WHERE id=?",
            (case_id,),
        ).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        if case["status"] in {"APPROVED", "DENIED"}:
            raise HTTPException(
                409,
                "The case is already resolved; prospective SLA breach predictions "
                "are limited to unresolved cases.",
            )
        unresolved_count = db.execute(
            "SELECT COUNT(*) AS count FROM documents WHERE case_id=? AND status!='VERIFIED'",
            (case_id,),
        ).fetchone()["count"]
        failure_count = db.execute(
            "SELECT COUNT(*) AS count FROM workflow_failures WHERE case_id=?",
            (case_id,),
        ).fetchone()["count"]
        pending_task_count = db.execute(
            "SELECT COUNT(*) AS count FROM manual_follow_ups WHERE case_id=? AND status='OPEN'",
            (case_id,),
        ).fetchone()["count"]
    try:
        deadline = parse_case_deadline(case["deadline"])
    except InvalidCaseDeadline as error:
        raise HTTPException(
            422, str(error)
        ) from error
    hours_remaining = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
    if hours_remaining <= 0:
        raise HTTPException(
            409,
            "The case deadline has passed; use deterministic SLA status handling "
            "instead of a prospective breach prediction.",
        )
    try:
        return (prediction_function or predict_sla_breach_risk)(
            {
                "hours_remaining": hours_remaining,
                "payer": case["payer"],
                "service": case["service"],
                "case_status": case["status"],
                "unresolved_document_count": unresolved_count,
                "workflow_failure_count": failure_count,
                "pending_task_count": pending_task_count,
            },
            case_id,
        )
    except InvalidRiskFeatures as error:
        raise HTTPException(422, str(error)) from error
    except ModelUnavailableError as error:
        raise HTTPException(503, str(error)) from error
