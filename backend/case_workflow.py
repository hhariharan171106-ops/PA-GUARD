import asyncio
import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from fastapi import HTTPException
from typing import Callable

from agents import run_sla_rag
from case_deadline import InvalidCaseDeadline, parse_case_deadline
from database import connect
from escalation_agents import run_escalation_rag
from evidence_prediction_model import predict_evidence_gaps
from evidence_agents import run_evidence_rag
from rag import (
    OLLAMA_MODEL,
    _OLLAMA_PROMPT_VERSION,
    build_case_explanation_prompt,
    deterministic_agent_explanations,
    generate_case_explanation_async,
)
from recovery_agents import run_recovery_rag
from sla_risk_service import predict_case_sla_risk
from sla_deterministic_risk import assess_case_operational_risk

logger = logging.getLogger(__name__)
DETERMINISTIC_EXPLANATION_ENGINE = "deterministic-case-facts-v1"
_EXPLANATION_CACHE_TTL_SECONDS = 600
_EXPLANATION_CACHE_LIMIT = 128
_EXPLANATION_MAX_IN_FLIGHT = 2
_EXPLANATION_CACHE: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_EXPLANATION_FAILURES: dict[str, dict] = {}
_EXPLANATION_TASKS: dict[str, asyncio.Task] = {}
_EXPLANATION_REQUEST_IDS: dict[str, str] = {}


def build_deterministic_case_explanation(facts: dict) -> str:
    """Explain only observed workflow fields; this is independent of Ollama."""
    prediction = facts.get("sla_risk_prediction") or {}
    operational = prediction.get("operational_risk") or {}
    lines = ["Case summary"]
    if prediction.get("available") and isinstance(prediction.get("risk_score"), (int, float)):
        probability = prediction.get("breach_probability")
        percent = float(probability) * 100 if isinstance(probability, (int, float)) else float(prediction["risk_score"])
        lines.append(
            f"Risk estimate: The experimental model marks this case {str(prediction.get('risk_level', 'unknown')).lower()} risk "
            f"({percent:.1f}% estimated chance of a deadline breach). It was trained on synthetic data "
            "and has not been validated for real cases, so treat this as an estimate only."
        )
    else:
        lines.append("Risk estimate: No model estimate is available for this case.")

    remaining = operational.get("remaining_hours")
    time_text = f"{remaining:g} hours remain" if isinstance(remaining, (int, float)) else "remaining SLA time is unavailable"
    operational_label = "deadline passed" if operational.get("breached") else str(operational.get("risk_category", "unavailable")).replace("_", " ").lower()
    reasons = operational.get("reasons") or ["No operational risk reasons were returned."]
    lines.append(
        f"Deadline check: {operational_label}; {time_text}. "
        f"Reason: {' '.join(str(reason) for reason in reasons[:3])}"
    )
    count = facts.get("unverified_document_count", 0)
    lines.append(
        f"Documents: {count} document(s) still need human verification. "
        "Text matches and model predictions do not confirm that a document is clinically sufficient."
    )
    lines.append(f"Case status: {str(facts.get('case_status') or 'unavailable').replace('_', ' ')}.")

    evidence = facts.get("evidence_items") or []
    if evidence:
        lines.append("Document checks: " + "; ".join(
            f"{item.get('requirement', 'Requirement')}: {str(item.get('status', 'UNKNOWN')).replace('_', ' ')}"
            for item in evidence[:8]
        ) + ".")
    escalation = facts.get("escalation") or {}
    if escalation:
        level = escalation.get("escalation_level", "unavailable")
        lines.append(
            f"Escalation review: {level.replace('_', ' ').lower()}; "
            f"{'threshold reached' if escalation.get('escalation_required') else 'no configured threshold reached'}. "
            "An ML estimate does not trigger escalation."
        )
    lines.append(
        "Next step: " + str(facts.get("recommended_next_action") or
        "Route unresolved items to an authorized human reviewer under the configured workflow.")
    )
    return "\n".join(lines)


def _case_explanation_facts(workflow_result: dict) -> dict:
    summary = workflow_result.get("summary") or {}
    stages = workflow_result.get("stages") or {}
    sla_stage = stages.get("sla_risk_prediction") or {}
    escalation_stage = stages.get("escalation_agent") or {}
    return {
        "case_status": summary.get("case_status"),
        "case_risk": summary.get("sla_status"),
        "deadline": summary.get("deadline"),
        "unverified_document_count": summary.get("unverified_document_count", 0),
        "evidence_items": summary.get("evidence_items", []),
        "sla_risk_prediction": sla_stage.get("result") or {},
        "escalation": escalation_stage.get("result") or summary.get("escalation") or {},
        "recommended_next_action": summary.get("recommended_next_action"),
        "workflow_fingerprint": summary.get("workflow_fingerprint"),
    }


def _explanation_cache_key(case_id: str, facts: dict) -> str:
    data = {
        "case_partition": hashlib.sha256(case_id.encode()).hexdigest(),
        "prompt_version": _OLLAMA_PROMPT_VERSION,
        "model": OLLAMA_MODEL,
        "prompt_facts_hash": hashlib.sha256(build_case_explanation_prompt(facts).encode()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _case_context_fingerprint(case_id: str) -> str:
    """Hash operational inputs that can make a previously generated explanation stale."""
    with connect() as db:
        case = db.execute(
            "SELECT id,status,risk,deadline,payer,service FROM cases WHERE id=?", (case_id,)
        ).fetchone()
        if not case:
            raise ValueError("Case not found")
        facts = {
            "case": dict(case),
            "documents": [dict(row) for row in db.execute(
                "SELECT id,status,processing_status,ocr_status,processed_at FROM documents WHERE case_id=? ORDER BY id",
                (case_id,),
            )],
            "assignment": dict(db.execute(
                "SELECT specialist_id,supervisor_id,manager_id,updated_at FROM pa_case_assignments WHERE case_id=?",
                (case_id,),
            ).fetchone() or {}),
            "escalations": [dict(row) for row in db.execute(
                "SELECT id,to_staff_id,to_role,status,updated_at FROM pa_escalations WHERE case_id=? ORDER BY id",
                (case_id,),
            )],
            "sla_settings": dict(db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone() or {}),
            "workflow_failures": db.execute(
                "SELECT COUNT(*) FROM workflow_failures WHERE case_id=?", (case_id,)
            ).fetchone()[0],
            "open_followups": db.execute(
                "SELECT COUNT(*) FROM manual_follow_ups WHERE case_id=? AND status='OPEN'", (case_id,)
            ).fetchone()[0],
        }
        policy_rows = db.execute(
            """SELECT id,policy_id,version,approved,effective_date,expires_at,content,
                      verification_status,source_verified_at
               FROM policy_documents WHERE (payer IS NULL OR payer=?)
                 AND (service_type IS NULL OR service_type=?) ORDER BY id""",
            (case["payer"], case["service"]),
        ).fetchall()
        facts["policies"] = [dict(row) for row in policy_rows]
    return hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _cached_explanation(key: str) -> dict | None:
    cached = _EXPLANATION_CACHE.get(key)
    if not cached:
        return None
    expires_at, value = cached
    if expires_at <= time.monotonic():
        _EXPLANATION_CACHE.pop(key, None)
        return None
    _EXPLANATION_CACHE.move_to_end(key)
    return value


def _store_explanation_failure(key: str, value: dict) -> None:
    _EXPLANATION_FAILURES[key] = value
    while len(_EXPLANATION_FAILURES) > _EXPLANATION_CACHE_LIMIT:
        _EXPLANATION_FAILURES.pop(next(iter(_EXPLANATION_FAILURES)))


def _audit_explanation(case_id: str, action: str, request_id: str, result: dict) -> None:
    diagnostics = result.get("diagnostics", {})
    detail = json.dumps({
        "request_id": request_id,
        "model": diagnostics.get("model", OLLAMA_MODEL),
        "elapsed_ms": diagnostics.get("elapsed_ms"),
        "load_duration_ms": diagnostics.get("load_duration_ms"),
        "prompt_eval_count": diagnostics.get("prompt_eval_count"),
        "eval_count": diagnostics.get("eval_count"),
        "failure_category": diagnostics.get("failure_category"),
        "retry_count": diagnostics.get("retry_count", 0),
        "cache_hit": False,
        "deterministic_fallback_used": not result.get("available"),
    }, sort_keys=True)
    try:
        with connect() as db:
            db.execute(
                """INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type)
                   VALUES(?,?,?,?,?,?)""",
                (case_id, datetime.now(timezone.utc).isoformat(), "Ollama Explanation Service", action, detail, "SYSTEM_AI_EXPLANATION_TELEMETRY"),
            )
    except Exception:
        logger.exception("Could not audit AI explanation request_id=%s", request_id)


async def _run_case_explanation(key: str, case_id: str, facts: dict, request_id: str) -> None:
    try:
        result = await generate_case_explanation_async(facts, request_id=request_id)
        if result.get("available"):
            public_result = {
                "status": "READY",
                "explanation": result["explanation"],
                "engine": result["engine"],
                "diagnostics": result["diagnostics"],
                "request_id": request_id,
            }
            _EXPLANATION_CACHE[key] = (time.monotonic() + _EXPLANATION_CACHE_TTL_SECONDS, public_result)
            _EXPLANATION_CACHE.move_to_end(key)
            while len(_EXPLANATION_CACHE) > _EXPLANATION_CACHE_LIMIT:
                _EXPLANATION_CACHE.popitem(last=False)
            _EXPLANATION_FAILURES.pop(key, None)
            _audit_explanation(case_id, "CASE_AI_EXPLANATION_GENERATED", request_id, result)
        else:
            _store_explanation_failure(key, {
                "status": "UNAVAILABLE",
                "detail": result.get("detail", "AI-enhanced explanation is unavailable."),
                "diagnostics": result.get("diagnostics", {}),
                "request_id": request_id,
            })
            _audit_explanation(case_id, "CASE_AI_EXPLANATION_GENERATION_FAILED", request_id, result)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        safe_result = {
            "available": False,
            "detail": "AI-enhanced explanation could not be generated.",
            "diagnostics": {
                "request_id": request_id, "model": OLLAMA_MODEL,
                "failure_category": type(error).__name__, "retry_count": 0,
                "cache_hit": False,
            },
        }
        _store_explanation_failure(key, {
            "status": "UNAVAILABLE", "detail": safe_result["detail"],
            "diagnostics": safe_result["diagnostics"], "request_id": request_id,
        })
        _audit_explanation(case_id, "CASE_AI_EXPLANATION_GENERATION_FAILED", request_id, safe_result)
        logger.exception("AI explanation task failed request_id=%s", request_id)
    finally:
        _EXPLANATION_TASKS.pop(key, None)
        _EXPLANATION_REQUEST_IDS.pop(key, None)


def _load_workflow_for_explanation(case_id: str) -> dict:
    with connect() as db:
        row = db.execute(
            "SELECT result_json FROM case_workflow_state WHERE case_id=?", (case_id,)
        ).fetchone()
    if not row:
        raise ValueError("Run the case workflow before requesting an AI-enhanced explanation.")
    result = json.loads(row["result_json"])
    if not isinstance(result, dict) or not isinstance(result.get("summary"), dict):
        raise ValueError("The saved case analysis is unavailable; rerun the workflow.")
    facts = _case_explanation_facts(result)
    if facts.get("workflow_fingerprint") != _case_context_fingerprint(case_id):
        raise RuntimeError("Case data changed since analysis; rerun the workflow before requesting an explanation.")
    return facts


async def request_case_explanation(case_id: str) -> dict:
    try:
        facts = _load_workflow_for_explanation(case_id)
    except ValueError:
        raise
    except RuntimeError as error:
        return {"status": "STALE", "detail": str(error)}
    key = _explanation_cache_key(case_id, facts)
    cached = _cached_explanation(key)
    if cached:
        logger.info("Ollama explanation cache hit case_key=%s", key[:12])
        return {**cached, "cache_hit": True}
    existing = _EXPLANATION_TASKS.get(key)
    if existing and not existing.done():
        return {"status": "GENERATING", "request_id": _EXPLANATION_REQUEST_IDS.get(key)}
    if len([task for task in _EXPLANATION_TASKS.values() if not task.done()]) >= _EXPLANATION_MAX_IN_FLIGHT:
        return {"status": "UNAVAILABLE", "detail": "Local explanation capacity is busy; the deterministic explanation remains available."}
    _EXPLANATION_FAILURES.pop(key, None)
    request_id = str(uuid.uuid4())
    task = asyncio.create_task(_run_case_explanation(key, case_id, facts, request_id))
    _EXPLANATION_TASKS[key] = task
    _EXPLANATION_REQUEST_IDS[key] = request_id
    return {"status": "GENERATING", "request_id": request_id}


async def case_explanation_status(case_id: str) -> dict:
    try:
        facts = _load_workflow_for_explanation(case_id)
    except ValueError:
        return {"status": "UNAVAILABLE", "detail": "No current case analysis is available."}
    except RuntimeError as error:
        return {"status": "STALE", "detail": str(error)}
    key = _explanation_cache_key(case_id, facts)
    cached = _cached_explanation(key)
    if cached:
        return {**cached, "cache_hit": True}
    task = _EXPLANATION_TASKS.get(key)
    if task and not task.done():
        return {"status": "GENERATING", "request_id": _EXPLANATION_REQUEST_IDS.get(key)}
    return _EXPLANATION_FAILURES.get(key, {"status": "NOT_REQUESTED"})


class WorkflowRetryUnavailableError(Exception):
    pass


def calculate_sla_risk_assessment(
    case_id: str,
    prediction_function: Callable | None = None,
    operational_risk_function: Callable | None = None,
) -> dict:
    """Always compute operational risk; attach an ML estimate only when valid/available."""
    operational = (operational_risk_function or assess_case_operational_risk)(
        case_id, connection_factory=connect
    )
    predictor = prediction_function or predict_case_sla_risk
    try:
        prediction = predictor(case_id, connection_factory=connect)
    except HTTPException as error:
        if error.status_code not in {409, 503}:
            raise
        prediction = {
            "case_id": case_id,
            "available": False,
            "predicted_sla_breach": None,
            "breach_probability": None,
            "risk_level": "NEEDS_REVIEW",
            "risk_score": None,
            "score_meaning": "No prospective ML estimate is available for this case.",
            "factors": [],
            "reliable": False,
            "reliability_note": str(error.detail),
            "training_data_source": "unavailable",
        }
    prediction["operational_risk"] = operational
    return prediction


def _record_stage_failure(
    case_id: str, stage: str, actor: str, error: Exception
) -> str:
    safe_error = f"{stage.replace('_', ' ').title()} failed ({type(error).__name__})."
    logger.exception("Case workflow stage failed (case_id=%s, stage=%s)", case_id, stage)
    _record_stage_issue(case_id, stage, actor, safe_error)
    return safe_error


def _record_stage_issue(case_id: str, stage: str, actor: str, safe_error: str) -> None:
    occurred_at = datetime.now(timezone.utc).isoformat()
    error_code = f"CASE_WORKFLOW_{stage.upper()}"
    with connect() as db:
        db.execute(
            """INSERT INTO workflow_failures
               (case_id,occurred_at,error_code,error_details,workflow_state)
               VALUES(?,?,?,?,?)""",
            (case_id, occurred_at, error_code, safe_error, stage),
        )
        db.execute(
            """INSERT INTO audit_events
               (case_id,timestamp,actor,action,detail,decision_type)
               VALUES(?,?,?,?,?,?)""",
            (
                case_id,
                occurred_at,
                actor,
                "CASE_WORKFLOW_STAGE_FAILED",
                json.dumps({"stage": stage, "error": safe_error}, sort_keys=True),
                "SYSTEM_WORKFLOW_FAILURE",
            ),
        )


def _record_stage_skip(
    case_id: str, stage: str, actor: str, reason: str, retryable: bool = False
) -> dict:
    occurred_at = datetime.now(timezone.utc).isoformat()
    logger.warning(
        "Case workflow stage skipped (case_id=%s, stage=%s, reason=%s)",
        case_id,
        stage,
        reason,
    )
    with connect() as db:
        db.execute(
            """INSERT INTO audit_events
               (case_id,timestamp,actor,action,detail,decision_type)
               VALUES(?,?,?,?,?,?)""",
            (
                case_id,
                occurred_at,
                actor,
                "CASE_WORKFLOW_STAGE_SKIPPED",
                json.dumps({"stage": stage, "reason": reason}, sort_keys=True),
                "SYSTEM_WORKFLOW_REQUIRES_DATA_CORRECTION",
            ),
        )
    return {"status": "skipped", "reason": reason, "retryable": retryable}


def _run_stage(
    case_id: str, stage: str, actor: str, action: Callable[[], dict]
) -> dict:
    try:
        with deterministic_agent_explanations():
            return {"status": "completed", "result": action()}
    except InvalidCaseDeadline as error:
        return _record_stage_skip(case_id, stage, actor, str(error), retryable=True)
    except Exception as error:
        return {
            "status": "failed",
            "error": _record_stage_failure(case_id, stage, actor, error),
        }


def _run_stage_or_reuse(
    case_id: str,
    stage: str,
    actor: str,
    action: Callable[[], dict],
    previous_stages: dict,
    retry_failed_only: bool,
    force: bool = False,
) -> dict:
    previous = previous_stages.get(stage)
    if retry_failed_only and previous and previous["status"] != "failed" and not force:
        if not (previous.get("status") == "skipped" and previous.get("retryable")):
            return previous
    return _run_stage(case_id, stage, actor, action)


def _evidence_summary(finding: dict | None, stage_status: str) -> list[dict]:
    if not finding or stage_status != "completed":
        return []

    found = finding.get("found_documents", [])
    possible = finding.get("possible_evidence_matches", [])
    missing = set(finding.get("missing_documents", []))
    unreadable = finding.get("unreadable_documents", [])
    requirements = finding.get("required_documents", [])
    validations_by_req = {
        item.get("requirement"): item
        for item in finding.get("evidence_validations", [])
        if isinstance(item, dict)
    }
    items: list[dict] = []
    for requirement in requirements:
        exact_matches = [
            match for match in found if match["required_document"] == requirement
        ]
        possible_matches = [
            match for match in possible if match["required_document"] == requirement
        ]
        if exact_matches or possible_matches:
            status = "NEEDS_REVIEW"
            support = exact_matches + possible_matches
            explanation = (
                "Requirement label text was matched in OCR output; this is not "
                "clinical verification."
            )
        elif requirement in missing:
            status = "MISSING"
            support = []
            explanation = (
                "Not identified in the successfully processed documents; a human "
                "reviewer must confirm whether it is actually missing."
            )
        elif unreadable:
            support = []
            if all(item.get("ocr_status") == "FAILED" for item in unreadable):
                status = "OCR_FAILED"
                explanation = (
                    "Evidence cannot be assessed because one or more documents "
                    "failed OCR; failed OCR is not evidence of absence."
                )
            else:
                status = "NOT_VERIFIABLE"
                explanation = (
                    "Evidence cannot be assessed until pending or unreadable "
                    "documents are resolved."
                )
        else:
            status = "NOT_VERIFIABLE"
            support = []
            explanation = (
                "The available text does not establish this requirement; human "
                "review is required."
            )
        items.append(
            {
                "requirement": requirement,
                "status": status,
                "validation_status": (
                    validations_by_req.get(requirement, {}).get("validation_status")
                    or ("MISSING" if requirement in missing else "UNCERTAIN")
                ),
                "supporting_documents": [
                    {
                        "filename": match.get("document_name", ""),
                        "category": match.get("category", ""),
                        "page_number": match.get("page_number"),
                        "excerpt": match.get("excerpt", ""),
                    }
                    for match in support
                ],
                "explanation": explanation,
                "human_review_required": (
                    validations_by_req.get(requirement, {}).get("human_review_required", status != "PRESENT")
                ),
            }
        )
    return items


def _resolve_responsible_staff(assignment: dict | None, escalations: list[dict]) -> tuple[dict | None, dict | None]:
    """Resolve an operational owner only from active assignment/escalation records."""
    recorded = assignment or {}
    responsible = None
    for slot, expected_role in (
        ("specialist", "SPECIALIST"),
        ("supervisor", "SUPERVISOR"),
        ("manager", "MANAGER"),
    ):
        if (
            recorded.get(f"{slot}_id")
            and recorded.get(f"{slot}_active")
            and recorded.get(f"{slot}_role") == expected_role
        ):
            responsible = {
                "name": recorded.get(f"{slot}_name"),
                "role": recorded.get(f"{slot}_role"),
                "source": "case assignment",
            }
            break
    latest = escalations[0] if escalations else None
    active_escalation = None
    if latest:
        assignee = {
            "name": latest.get("to_staff_name"),
            "role": latest.get("to_staff_role") or latest.get("to_role"),
            "status": latest["status"],
        }
        active_escalation = {**latest, "assignee": assignee}
        if latest["status"] == "ACCEPTED":
            responsible = {**assignee, "source": "accepted escalation"}
    return responsible, active_escalation


def run_case_workflow(
    case_id: str, actor: str, retry_failed_only: bool = False
) -> dict:
    with connect() as db:
        case_row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case_row:
            raise ValueError("Case not found")
        stored_state = db.execute(
            "SELECT result_json FROM case_workflow_state WHERE case_id=?",
            (case_id,),
        ).fetchone()
        prior_failures = db.execute(
            "SELECT id FROM workflow_failures WHERE case_id=? ORDER BY occurred_at DESC",
            (case_id,),
        ).fetchall()
    if retry_failed_only and not stored_state:
        raise WorkflowRetryUnavailableError(
            "No previous AI workflow results are available to retry."
        )
    previous_result = json.loads(stored_state["result_json"]) if stored_state else {}
    previous_stages = previous_result.get("stages", {})
    if retry_failed_only and not any(
        stage.get("status") == "failed"
        or (stage.get("status") == "skipped" and stage.get("retryable"))
        for stage in previous_stages.values()
    ):
        raise WorkflowRetryUnavailableError(
            "There are no failed AI stages to retry."
        )

    previous_evidence_stage = previous_stages.get("evidence_agent", {})
    previous_evidence_prediction_stage = previous_stages.get("evidence_prediction", {})
    previous_sla_stage = previous_stages.get("sla_agent", {})
    evidence_stage = _run_stage_or_reuse(
        case_id,
        "evidence_agent",
        actor,
        lambda: run_evidence_rag(case_id),
        previous_stages,
        retry_failed_only,
    )
    evidence_reran = (
        not retry_failed_only
        or previous_evidence_stage.get("status") == "failed"
        or previous_evidence_stage.get("retryable") is True
        or "evidence_agent" not in previous_stages
    )
    evidence_result = evidence_stage.get("result")
    if evidence_result:
        def predict_case_evidence() -> dict:
            with connect() as db:
                documents_for_prediction = [
                    dict(row)
                    for row in db.execute(
                        """SELECT category,status,processing_status,ocr_status,extracted_text
                           FROM documents WHERE case_id=? ORDER BY id""",
                        (case_id,),
                    )
                ]
            return predict_evidence_gaps(
                evidence_result["required_documents"],
                documents_for_prediction,
                evidence_result,
            )

        evidence_prediction_stage = _run_stage_or_reuse(
            case_id,
            "evidence_prediction",
            actor,
            predict_case_evidence,
            previous_stages,
            retry_failed_only,
            force=retry_failed_only
            and (
                previous_evidence_stage.get("status") == "failed"
                or previous_evidence_stage.get("retryable") is True
            ),
        )
    else:
        evidence_prediction_stage = {
            "status": "skipped",
            "reason": "Evidence Agent results are unavailable.",
        }
    sla_stage = _run_stage_or_reuse(
        case_id,
        "sla_agent",
        actor,
        lambda: run_sla_rag(case_id),
        previous_stages,
        retry_failed_only,
    )
    evidence_prediction_reran = (
        not retry_failed_only
        or previous_evidence_prediction_stage.get("status") == "failed"
        or previous_evidence_prediction_stage.get("retryable") is True
        or evidence_reran
        or "evidence_prediction" not in previous_stages
    )
    sla_reran = (
        not retry_failed_only
        or previous_sla_stage.get("status") == "failed"
        or previous_sla_stage.get("retryable") is True
        or "sla_agent" not in previous_stages
    )

    def calculate_sla_risk() -> dict:
        return calculate_sla_risk_assessment(case_id)

    previous_sla_risk_stage = previous_stages.get("sla_risk_prediction")
    if (
        retry_failed_only
        and previous_sla_risk_stage
        and previous_sla_risk_stage["status"] != "failed"
        and not previous_sla_risk_stage.get("retryable")
    ):
        sla_risk_stage = previous_sla_risk_stage
    else:
        try:
            sla_risk_stage = {
                "status": "completed",
                "result": calculate_sla_risk(),
            }
        except HTTPException as error:
            if error.status_code == 409:
                sla_risk_stage = {
                    "status": "skipped",
                    "reason": str(error.detail),
                }
            elif error.status_code == 422 and "case deadline" in str(error.detail).lower():
                sla_risk_stage = _record_stage_skip(
                    case_id,
                    "sla_risk_prediction",
                    actor,
                    str(error.detail),
                    retryable=True,
                )
            else:
                sla_risk_stage = {
                    "status": "failed",
                    "error": _record_stage_failure(
                        case_id, "sla_risk_prediction", actor, error
                    ),
                }
        except Exception as error:
            sla_risk_stage = {
                "status": "failed",
                "error": _record_stage_failure(
                    case_id, "sla_risk_prediction", actor, error
                ),
            }
    sla_risk_reran = (
            not retry_failed_only
            or not previous_sla_risk_stage
            or previous_sla_risk_stage.get("status") == "failed"
            or previous_sla_risk_stage.get("retryable") is True
    )
    previous_escalation_stage = previous_stages.get("escalation_agent", {})
    escalation_stage = _run_stage_or_reuse(
        case_id,
        "escalation_agent",
        actor,
        lambda: run_escalation_rag(case_id, actor, approve_notification=False),
        previous_stages,
        retry_failed_only,
    )
    escalation_reran = (
        not retry_failed_only
        or previous_escalation_stage.get("status") == "failed"
        or previous_escalation_stage.get("retryable") is True
        or "escalation_agent" not in previous_stages
    )
    previous_recovery_stage = previous_stages.get("recovery_agent", {})
    should_run_recovery = bool(prior_failures) and (
        not retry_failed_only
        or previous_recovery_stage.get("status") == "failed"
        or previous_recovery_stage.get("status") == "skipped"
    )
    if should_run_recovery:
        recovery_stage = _run_stage_or_reuse(
            case_id,
            "recovery_agent",
            actor,
            lambda: run_recovery_rag(
                case_id, actor, error_code=None, approve_recovery=False, preview_only=True
            ),
            previous_stages,
            retry_failed_only,
            force=retry_failed_only
            and previous_recovery_stage.get("status") == "skipped",
        )
    elif retry_failed_only and previous_recovery_stage:
        recovery_stage = previous_recovery_stage
    else:
        recovery_stage = {
            "status": "skipped",
            "reason": "No prior workflow failure is recorded for this case.",
        }
    recovery_reran = should_run_recovery

    evidence_items = _evidence_summary(
        evidence_result, evidence_stage["status"]
    )
    with connect() as db:
        documents = [
            dict(row)
            for row in db.execute(
                """SELECT id,case_id,category,original_filename,name,status,reviewed_by,
                          processing_status,ocr_status,processing_error,extracted_text,
                          page_text,file_size,uploaded_at
                   FROM documents WHERE case_id=? ORDER BY id""",
                (case_id,),
            )
        ]
        latest_case = dict(
            db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        )
        assignment_row = db.execute(
            """SELECT a.specialist_id,a.supervisor_id,a.manager_id,
                      s.display_name specialist_name,s.role specialist_role,s.active specialist_active,
                      v.display_name supervisor_name,v.role supervisor_role,v.active supervisor_active,
                      m.display_name manager_name,m.role manager_role,m.active manager_active
               FROM pa_case_assignments a
               LEFT JOIN pa_staff s ON s.id=a.specialist_id
               LEFT JOIN pa_staff v ON v.id=a.supervisor_id
               LEFT JOIN pa_staff m ON m.id=a.manager_id
               WHERE a.case_id=?""", (case_id,)
        ).fetchone()
        escalation_rows = [dict(row) for row in db.execute(
            """SELECT e.id,e.status,e.to_role,e.created_at,t.display_name to_staff_name,t.role to_staff_role
               FROM pa_escalations e LEFT JOIN pa_staff t ON t.id=e.to_staff_id
               WHERE e.case_id=? AND e.status IN ('ROUTED','ACCEPTED') ORDER BY e.id DESC""", (case_id,)
        )]

    document_summaries = [
        {
            "document_id": document["id"],
            "filename": document.get("original_filename") or document["name"],
            "category": document.get("category") or "",
            "verification_status": document.get("status") or "UNKNOWN",
            "reviewer": document.get("reviewed_by"),
            "ocr_status": document.get("ocr_status") or "UNKNOWN",
            "processing_status": document.get("processing_status") or "UNKNOWN",
            "extracted_text_available": bool(
                (document.get("extracted_text") or "").strip()
            ),
            "processing_error": document.get("processing_error") or "",
            "file_size": document.get("file_size") or 0,
            "uploaded_at": document.get("uploaded_at"),
            "page_text_available": bool((document.get("page_text") or "").strip()),
        }
        for document in documents
    ]

    responsible_staff, active_escalation = _resolve_responsible_staff(
        dict(assignment_row) if assignment_row else None, escalation_rows
    )
    unverified_documents = [
        document for document in document_summaries
        if document["verification_status"] != "VERIFIED"
    ]

    escalation_result = escalation_stage.get("result")
    sla_result = sla_stage.get("result")
    deadline_correction_required = any(
        stage.get("status") == "skipped"
        and "deadline" in stage.get("reason", "").lower()
        for stage in (sla_stage, sla_risk_stage, escalation_stage)
    )
    if deadline_correction_required:
        recommended_next_action = (
            "Ask a manager to correct the case deadline using an ISO-8601 timestamp "
            "with a timezone, then rerun the workflow."
        )
    elif escalation_stage["status"] == "completed" and escalation_result.get(
        "escalation_required"
    ):
        recommended_next_action = escalation_result.get("recommended_action")
    elif evidence_result:
        recommended_next_action = evidence_result.get("recommended_action")
    elif sla_result:
        recommended_next_action = sla_result.get("recommended_action")
    else:
        recommended_next_action = (
            "Review the failed workflow stages and route the case to an authorized human reviewer."
        )

    prediction_result = sla_risk_stage.get("result") or {}
    ml_priority_human_review = bool(
        prediction_result.get("available") and prediction_result.get("risk_level") == "HIGH"
    )
    if ml_priority_human_review and latest_case["status"] not in {"APPROVED", "DENIED"}:
        formal_escalation_status = (
            "A configured formal escalation threshold has been reached; follow the approved escalation workflow."
            if escalation_result and escalation_result.get("escalation_required")
            else "Configured formal escalation thresholds have not been met."
        )
        recommended_next_action = (
            "Priority human review is recommended by the experimental SLA model. "
            f"Review outstanding evidence. {formal_escalation_status}"
        )

    explanation_facts = {
        "case_id": case_id,
        "case_status": latest_case["status"],
        "case_risk": latest_case["risk"],
        "deadline": _deadline_summary(latest_case["deadline"]),
        "unverified_document_count": len(unverified_documents),
        "documents": [
            {
                key: document[key]
                for key in (
                    "filename",
                    "category",
                    "ocr_status",
                    "extracted_text_available",
                    "processing_error",
                )
            }
            for document in document_summaries
        ],
        "evidence_items": evidence_items,
        "evidence_stage_status": evidence_stage["status"],
        "evidence_prediction": evidence_prediction_stage.get("result")
        or {"stage_status": evidence_prediction_stage["status"]},
        "sla": sla_result or {"stage_status": sla_stage["status"]},
        "sla_risk_prediction": sla_risk_stage.get("result")
        or {"stage_status": sla_risk_stage["status"]},
        "escalation": escalation_result
        or {"stage_status": escalation_stage["status"]},
        "recovery": recovery_stage.get("result")
        or {"stage_status": recovery_stage["status"]},
        "failed_stages": {
            stage: data.get("error")
            for stage, data in (
                ("evidence_agent", evidence_stage),
                ("evidence_prediction", evidence_prediction_stage),
                ("sla_agent", sla_stage),
                ("sla_risk_prediction", sla_risk_stage),
                ("escalation_agent", escalation_stage),
                ("recovery_agent", recovery_stage),
            )
            if data["status"] == "failed"
        },
        "recommended_next_action": recommended_next_action,
    }
    workflow_fingerprint = _case_context_fingerprint(case_id)
    explanation_facts["workflow_fingerprint"] = workflow_fingerprint
    llm_stage = {
        "status": "completed",
        "result": {
            "explanation": build_deterministic_case_explanation(explanation_facts),
            "engine": DETERMINISTIC_EXPLANATION_ENGINE,
        },
    }

    summary = {
        "case_id": case_id,
        "case_status": latest_case["status"],
        "sla_status": latest_case["risk"],
        "deadline": latest_case["deadline"],
        "documents": document_summaries,
        "unverified_documents": unverified_documents,
        "unverified_document_count": len(unverified_documents),
        "evidence_reference_count": len({
            (source.get("filename"), source.get("page_number"))
            for item in evidence_items
            for source in item.get("supporting_documents", [])
        }),
        "responsible_staff": responsible_staff,
        "active_escalation": active_escalation,
        "ml_priority_human_review_recommended": ml_priority_human_review,
        "workflow_fingerprint": workflow_fingerprint,
        "evidence_items": evidence_items,
        "unresolved_requirements": [
            item for item in evidence_items if item["status"] != "PRESENT"
        ],
        "human_review_items": [
            item["requirement"]
            for item in evidence_items
            if item["human_review_required"]
        ]
        + [
            f"OCR: {document['filename']}"
            for document in document_summaries
            if document["ocr_status"] != "COMPLETED"
        ]
        + [
            f"Workflow stage: {stage.replace('_', ' ')}"
            for stage, result in (
                ("evidence_agent", evidence_stage),
                ("evidence_prediction", evidence_prediction_stage),
                ("sla_agent", sla_stage),
                ("sla_risk_prediction", sla_risk_stage),
                ("escalation_agent", escalation_stage),
                ("recovery_agent", recovery_stage),
            )
            if result["status"] == "failed"
            or (
                result["status"] == "skipped"
                and stage in {"sla_agent", "sla_risk_prediction", "escalation_agent"}
                and "deadline" in result.get("reason", "").lower()
            )
        ]
        + (
            ["Escalation policy or threshold requires human review."]
            if escalation_result
            and escalation_result.get("escalation_level") == "NEEDS_REVIEW"
            else []
        ),
        "escalation": escalation_result,
        "escalation_reason": (
            escalation_result.get("reason")
            if escalation_result
            else escalation_stage.get("error", "Escalation result unavailable.")
        ),
        "recommended_next_action": recommended_next_action,
    }
    stage_statuses = {
        "evidence_agent": evidence_stage,
        "evidence_prediction": evidence_prediction_stage,
        "sla_agent": sla_stage,
        "sla_risk_prediction": sla_risk_stage,
        "escalation_agent": escalation_stage,
        "recovery_agent": recovery_stage,
        "llm_explanation": llm_stage,
        "summary": {"status": "completed", "result": summary},
    }
    has_ocr_failures = any(
        document["ocr_status"] != "COMPLETED" for document in document_summaries
    )
    required_stages = (
        "evidence_agent",
        "evidence_prediction",
        "sla_agent",
        "escalation_agent",
        "llm_explanation",
        "summary",
    )
    workflow_status = (
        "completed"
        if not has_ocr_failures
        and all(stage_statuses[name]["status"] == "completed" for name in required_stages)
        and all(
            stage["status"] in {"completed", "skipped"}
            for name, stage in stage_statuses.items()
            if name not in required_stages
        )
        else "partial"
    )
    completed_at = datetime.now(timezone.utc).isoformat()
    workflow_result = {
        "case_id": case_id,
        "workflow_status": workflow_status,
        "explanation_status": "DETERMINISTIC_READY",
        "stages": stage_statuses,
        "llm_explanation": llm_stage.get("result"),
        "summary": summary,
    }
    with connect() as db:
        db.execute(
            """INSERT INTO audit_events
               (case_id,timestamp,actor,action,detail,decision_type)
               VALUES(?,?,?,?,?,?)""",
            (
                case_id,
                completed_at,
                actor,
                "CASE_AI_WORKFLOW_FINISHED",
                json.dumps(
                    {
                        "workflow_status": workflow_status,
                        "stage_statuses": {
                            name: stage["status"]
                            for name, stage in stage_statuses.items()
                        },
                    },
                    sort_keys=True,
                ),
                "AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW",
            ),
        )
        db.execute(
            """INSERT INTO case_workflow_state (case_id,updated_at,result_json)
               VALUES(?,?,?)
               ON CONFLICT(case_id) DO UPDATE SET
                   updated_at=excluded.updated_at,
                   result_json=excluded.result_json""",
            (case_id, completed_at, json.dumps(workflow_result, sort_keys=True)),
        )
    return workflow_result


def _deadline_summary(value: object) -> str:
    try:
        return parse_case_deadline(value).isoformat()
    except InvalidCaseDeadline:
        return "Invalid or ambiguous stored deadline; manager correction required."
