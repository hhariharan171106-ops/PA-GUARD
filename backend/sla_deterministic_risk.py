"""Deterministic operational urgency from case state and configured SLA rules."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import HTTPException

from case_deadline import InvalidCaseDeadline, parse_case_deadline
from database import connect

RULE_VERSION = "sla-operational-risk-v1"
RESOLVED_STATUSES = {"APPROVED", "DENIED"}


def assess_case_operational_risk(
    case_id: str,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    now: datetime | None = None,
) -> dict:
    """Return reproducible urgency, retaining actual deadline status separately from ML."""
    with (connection_factory or connect)() as db:
        case_row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case_row:
            raise HTTPException(404, "Case not found")
        case = dict(case_row)
        settings_row = db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone()
        settings = dict(settings_row) if settings_row else {
            "warning_hours": 24, "urgent_hours": 8,
            "escalation_one_hours": 4, "escalation_two_hours": 0,
        }
        unresolved = db.execute(
            "SELECT COUNT(*) FROM documents WHERE case_id=? AND status!='VERIFIED'", (case_id,)
        ).fetchone()[0]
        failures = db.execute(
            "SELECT COUNT(*) FROM workflow_failures WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        followups = db.execute(
            "SELECT COUNT(*) FROM manual_follow_ups WHERE case_id=? AND status='OPEN'", (case_id,)
        ).fetchone()[0]
        assignment = db.execute(
            "SELECT specialist_id, supervisor_id, manager_id FROM pa_case_assignments WHERE case_id=?",
            (case_id,),
        ).fetchone()
        settings_change = db.execute(
            "SELECT MAX(changed_at) FROM settings_history"
        ).fetchone()[0]

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        deadline = parse_case_deadline(case["deadline"])
    except InvalidCaseDeadline as error:
        raise HTTPException(422, str(error)) from error
    remaining_seconds = (deadline - instant.astimezone(timezone.utc)).total_seconds()
    remaining_hours = remaining_seconds / 3600
    reasons: list[str] = []
    is_resolved = case["status"] in RESOLVED_STATUSES
    breached = remaining_seconds <= 0 and not is_resolved
    if is_resolved:
        category = "RESOLVED"
        reasons.append(f"Case is in terminal status {case['status']}.")
        action = "No SLA action is required for a resolved case."
    elif breached:
        category = "BREACHED"
        reasons.append("The applicable case deadline has passed.")
        action = "Review the breach, document the cause, and follow the configured escalation workflow."
    else:
        warning = max(0, int(settings["warning_hours"]))
        urgent = max(0, int(settings["urgent_hours"]))
        first = max(0, int(settings["escalation_one_hours"]))
        second = max(0, int(settings["escalation_two_hours"]))
        if remaining_hours <= second:
            category = "CRITICAL"
            reasons.append(f"Remaining time is at or below escalation tier 2 ({second} hours).")
        elif remaining_hours <= first:
            category = "HIGH"
            reasons.append(f"Remaining time is at or below escalation tier 1 ({first} hours).")
        elif remaining_hours <= urgent:
            category = "HIGH"
            reasons.append(f"Remaining time is at or below the urgent threshold ({urgent} hours).")
        elif remaining_hours <= warning:
            category = "MEDIUM"
            reasons.append(f"Remaining time is at or below the warning threshold ({warning} hours).")
        else:
            category = "LOW"
        if unresolved:
            reasons.append(f"{unresolved} document(s) remain unverified.")
        if failures:
            reasons.append(f"{failures} workflow failure(s) are open for review.")
        if followups:
            reasons.append(f"{followups} follow-up task(s) remain open.")
        if not assignment or not any(assignment):
            reasons.append("The case has no recorded staff assignment.")
        # Workflow blockers raise urgency by one tier without altering the deadline.
        if category == "LOW" and (unresolved or failures or followups or not assignment or not any(assignment)):
            category = "MEDIUM"
        if category == "MEDIUM" and (failures or followups) and remaining_hours <= warning:
            category = "HIGH"
        action = {
            "LOW": "Continue routine monitoring against the recorded deadline.",
            "MEDIUM": "Review outstanding evidence and follow-ups before the warning window closes.",
            "HIGH": "Prioritize this case and resolve blockers; notify the responsible owner.",
            "CRITICAL": "Escalate immediately through the configured escalation workflow.",
        }[category]

    # Tier 2 may be configured farther from the deadline than tier 1 (for
    # example, level 2 at 10h and level 1 at 2h). Either configured tier means
    # the formal escalation window has been reached.
    escalation_window_hours = max(
        0,
        int(settings["escalation_one_hours"]),
        int(settings["escalation_two_hours"]),
    )
    escalation_required = not is_resolved and (
        breached or remaining_hours <= escalation_window_hours
    )
    return {
        "case_id": case_id,
        "risk_category": category,
        "breached": breached,
        "resolved": is_resolved,
        "deadline": deadline.isoformat(),
        "remaining_seconds": round(remaining_seconds),
        "remaining_hours": round(remaining_hours, 2),
        "reasons": reasons or ["No configured urgency threshold or workflow blocker is active."],
        "recommended_action": action,
        "escalation_required": escalation_required,
        "configuration": {
            "rule_version": RULE_VERSION,
            "warning_hours": int(settings["warning_hours"]),
            "urgent_hours": int(settings["urgent_hours"]),
            "escalation_one_hours": int(settings["escalation_one_hours"]),
            "escalation_two_hours": int(settings["escalation_two_hours"]),
            "last_changed_at": settings_change,
        },
        "workflow_state": {
            "status": case["status"], "unverified_document_count": unresolved,
            "workflow_failure_count": failures, "open_follow_up_count": followups,
            "assigned": bool(assignment and any(assignment)),
        },
    }
