"""Deterministic submission readiness and explicitly simulated payer adapter."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from approval_api import case_access, current_user
from case_deadline import InvalidCaseDeadline, parse_case_deadline
from database import connect
from evidence_agents import _evidence_match
from sla_risk_service import predict_case_sla_risk

router = APIRouter(prefix="/api/cases/{case_id}/submission", tags=["submission"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalRequest(BaseModel):
    readiness_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=1000)


class DemoSubmitRequest(BaseModel):
    readiness_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=100)


def run_demo_adapter(case: dict, readiness: dict) -> dict:
    """Return a local simulation receipt; this never calls a payer service."""
    reference = f"DEMO-PA-{uuid.uuid4().hex[:12].upper()}"
    return {
        "status": "SUBMITTED_DEMO",
        "reference_id": reference,
        "response_summary": "Simulated local submission only. No insurer or payer endpoint was contacted; no payer response or authorization decision was received.",
    }


def _load_decision(db, case_id: str, include_predictions: bool = True) -> dict:
    case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(404, "Case not found")
    case = dict(case)
    today = date.today().isoformat()
    policy_columns = {row["name"] for row in db.execute("PRAGMA table_info(policy_documents)")}
    verification_select = (
        "verification_status" if "verification_status" in policy_columns
        else "'MOCK_DEMO' AS verification_status"
    )
    rows = [dict(row) for row in db.execute(
        """SELECT policy_id,document_type,title,effective_date,expires_at,version,
                  payer,service_type,source_reference,required_documents,""" + verification_select + """
           FROM policy_documents WHERE approved=1
             AND document_type IN ('PAYER_REQUIREMENT','DOCUMENT_CHECKLIST',
               'CLINICAL_DOCUMENTATION_RULE','AUTHORIZATION_GUIDELINE')
             AND (payer IS NULL OR payer=? OR payer='ALL')
             AND (service_type IS NULL OR service_type=? OR service_type='ALL')""",
        (case["payer"], case["service"]),
    )]
    applicable = []
    conflicts: list[str] = []
    grouped: dict[str, set[str]] = {}
    for row in rows:
        try:
            active = date.fromisoformat(row["effective_date"]) <= date.fromisoformat(today) and (
                not row["expires_at"] or date.fromisoformat(row["expires_at"]) >= date.fromisoformat(today)
            )
        except (TypeError, ValueError):
            conflicts.append(f"Requirement {row['policy_id']} has an invalid effective date.")
            continue
        if active:
            grouped.setdefault(row["policy_id"], set()).add(row["version"])
            applicable.append(row)
    for policy_id, versions in grouped.items():
        if len(versions) > 1:
            conflicts.append(f"Multiple active versions for requirement {policy_id}: {', '.join(sorted(versions))}.")
    policy_status = "VERIFIED_PUBLIC" if applicable and all(row.get("verification_status") == "VERIFIED_PUBLIC" for row in applicable) else "UNAVAILABLE_OR_UNVERIFIED"
    if applicable and policy_status != "VERIFIED_PUBLIC":
        conflicts.append("Applicable policy is unverified or demonstration-only; its requirements cannot authorize submission readiness.")

    policy_criteria: list[dict] = []
    human_reviewed: set[tuple[str, str, str]] = set()
    if policy_status == "VERIFIED_PUBLIC":
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "policy_criteria" in tables:
            for policy in applicable:
                policy_criteria.extend(dict(row) for row in db.execute(
                    """SELECT policy_id,policy_version,criterion_id,criterion_text FROM policy_criteria
                       WHERE policy_id=? AND policy_version=? AND verification_status='VERIFIED_PUBLIC'""",
                    (policy["policy_id"], policy["version"]),
                ))
        if not policy_criteria:
            conflicts.append("Verified policy has no criterion-level source provenance; submission readiness requires human review.")
        if "case_evidence_reviews" in tables:
            for review in db.execute(
                """SELECT policy_id,policy_version,criterion_id,assessment_status,review_status
                   FROM case_evidence_reviews WHERE case_id=?""", (case_id,)
            ):
                if review["review_status"] == "HUMAN_CONFIRMED" and review["assessment_status"] == "MATCH_POSSIBLE":
                    human_reviewed.add((review["policy_id"], review["policy_version"], review["criterion_id"]))
        if policy_criteria and any((item["policy_id"], item["policy_version"], item["criterion_id"]) not in human_reviewed for item in policy_criteria):
            conflicts.append("One or more verified policy criteria lack a human-confirmed supporting-evidence review.")
    evidence_review_status = "HUMAN_CONFIRMED" if policy_criteria and all((item["policy_id"], item["policy_version"], item["criterion_id"]) in human_reviewed for item in policy_criteria) else "PENDING_OR_UNAVAILABLE"

    required: list[str] = []
    for policy in applicable:
        try:
            configured = json.loads(policy["required_documents"] or "[]")
        except (TypeError, json.JSONDecodeError):
            configured = None
        if not isinstance(configured, list) or any(not isinstance(value, str) for value in configured):
            conflicts.append(f"Requirement {policy['policy_id']} has an invalid document checklist.")
            continue
        required.extend(value.strip() for value in configured if value.strip())
    required = list(dict.fromkeys(required))

    documents = [dict(row) for row in db.execute(
        "SELECT id,name,original_filename,category,ocr_status,processing_status,extracted_text,page_text,uploaded_at,processed_at FROM documents WHERE case_id=? ORDER BY id",
        (case_id,),
    )]
    findings = []
    missing = []
    possible = []
    unreadable = []
    for requirement in required:
        exact_matches = []
        possible_matches = []
        for document in documents:
            if document["ocr_status"] != "COMPLETED" or not (document["extracted_text"] or "").strip():
                unreadable.append({"document_id": document["id"], "filename": document["original_filename"] or document["name"], "ocr_status": document["ocr_status"] or "UNKNOWN"})
                continue
            try:
                pages = json.loads(document["page_text"] or "[]")
            except (TypeError, json.JSONDecodeError):
                pages = []
            if not isinstance(pages, list) or not pages:
                pages = [document["extracted_text"]]
            for page_number, page_text in enumerate(pages, 1):
                match_status, excerpt = _evidence_match(str(page_text or ""), requirement)
                if not match_status:
                    continue
                record = {"document_id": document["id"], "filename": document["original_filename"] or document["name"], "category": document["category"], "page_number": page_number, "excerpt": excerpt}
                (exact_matches if match_status == "FOUND_IN_TEXT" else possible_matches).append(record)
        if exact_matches:
            status = "FOUND_IN_TEXT"
            support = exact_matches
        elif possible_matches:
            status = "POSSIBLE_MATCH"
            support = possible_matches
            possible.append(requirement)
        elif unreadable:
            status = "NOT_VERIFIABLE"
            support = []
        else:
            status = "MISSING"
            support = []
            missing.append(requirement)
        findings.append({"requirement": requirement, "status": status, "provenance": support})

    case_data_missing = [name for name in ("payer", "service", "submission_date", "deadline") if not str(case.get(name) or "").strip()]
    deadline_valid = True
    try:
        deadline = parse_case_deadline(case["deadline"])
        remaining_hours = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
        settings = db.execute("SELECT warning_hours,urgent_hours FROM sla_settings WHERE id=1").fetchone()
        warning_hours = int(settings["warning_hours"]) if settings else 24
        urgent_hours = int(settings["urgent_hours"]) if settings else 8
        sla_status = "BREACHED" if remaining_hours <= 0 else "AT RISK" if remaining_hours <= warning_hours else "ON TRACK"
    except (InvalidCaseDeadline, KeyError, TypeError):
        deadline_valid = False
        remaining_hours = None
        sla_status = "UNKNOWN"
        if "deadline" not in case_data_missing:
            case_data_missing.append("deadline (valid ISO-8601 timestamp with timezone required)")

    prediction = {"available": False, "reason": "Not requested"}
    if include_predictions:
        try:
            prediction = predict_case_sla_risk(case_id)
        except HTTPException as error:
            prediction = {"available": False, "reason": str(error.detail)}

    # Include current input data in the approval freshness check without persisting document text in the readiness record.
    fingerprint_input = {
        "case": {key: case.get(key) for key in ("id", "payer", "service", "submission_date", "deadline", "status")},
        "documents": [{key: doc.get(key) for key in ("id", "category", "ocr_status", "processing_status", "uploaded_at", "processed_at")} | {"text_hash": hashlib.sha256((doc.get("extracted_text") or "").encode()).hexdigest()} for doc in documents],
        "requirements": [{key: row.get(key) for key in ("policy_id", "version", "effective_date", "expires_at", "required_documents")} for row in applicable],
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_input, sort_keys=True).encode()).hexdigest()
    if not applicable:
        conflicts.append("No active approved payer submission requirements apply to this case.")
    if missing or case_data_missing:
        status = "NOT_READY"
    elif conflicts or possible or unreadable or not deadline_valid or not required:
        status = "NEEDS_HUMAN_REVIEW"
    else:
        status = "READY_FOR_REVIEW"
    return {
        "case": {key: case[key] for key in ("id", "payer", "service", "submission_date", "deadline", "status")},
        "status": status,
        "required_evidence": findings,
        "missing_items": missing + case_data_missing,
        "conflicts": conflicts,
        "possible_matches": possible,
        "unreadable_documents": unreadable,
        "evidence_provenance": [item for finding in findings for item in finding["provenance"]],
        "sla_status": sla_status,
        "hours_remaining": remaining_hours,
        "sla_risk_prediction": prediction,
        "policy_mode": "AUTHORITATIVE" if policy_status == "VERIFIED_PUBLIC" else "DEMO_ONLY",
        "policy_status": policy_status,
        "evidence_review_status": evidence_review_status,
        "approval_status": "PENDING_MANAGER_APPROVAL",
        "required_actions": [message for message in conflicts],
        "blockers": list(missing) + list(case_data_missing) + list(conflicts),
        "warnings": ["SLA prediction is advisory and synthetic-model performance is not real-world validated."],
        "applicable_requirements": [{key: row.get(key) for key in ("policy_id", "title", "version", "source_reference", "verification_status")} for row in applicable],
        "fingerprint": fingerprint,
    }


def _require_scoped_case(case_id: str, user: dict) -> dict:
    with connect() as db:
        case = case_access(db, case_id, user)
        return case


@router.post("/readiness")
def evaluate_readiness(case_id: str, user: dict = Depends(current_user)):
    if user["role"] not in {"SPECIALIST", "SUPERVISOR", "MANAGER"}:
        raise HTTPException(403, "Role cannot review submission readiness")
    _require_scoped_case(case_id, user)
    with connect() as db:
        decision = _load_decision(db, case_id)
        current = db.execute("SELECT version FROM pa_submission_readiness WHERE case_id=?", (case_id,)).fetchone()
        version = int(current["version"] + 1) if current else 1
        evaluated_at = _now()
        db.execute("""INSERT INTO pa_submission_readiness(case_id,version,status,fingerprint,decision_json,evaluated_by,evaluated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET version=excluded.version,status=excluded.status,
            fingerprint=excluded.fingerprint,decision_json=excluded.decision_json,evaluated_by=excluded.evaluated_by,evaluated_at=excluded.evaluated_at""",
            (case_id, version, decision["status"], decision["fingerprint"], json.dumps({k:v for k,v in decision.items() if k!="fingerprint"}, sort_keys=True), user["id"], evaluated_at))
        db.execute("DELETE FROM pa_submission_approvals WHERE case_id=?", (case_id,))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",
            (case_id, evaluated_at, user["display_name"], "SUBMISSION_READINESS_EVALUATED", json.dumps({"version":version,"status":decision["status"],"missing_count":len(decision["missing_items"]),"conflict_count":len(decision["conflicts"])}), "DETERMINISTIC_READINESS"))
    public_decision = {key: value for key, value in decision.items() if key != "fingerprint"}
    return {**public_decision, "version": version, "evaluated_at": evaluated_at}


@router.get("/readiness")
def get_readiness(case_id: str, user: dict = Depends(current_user)):
    _require_scoped_case(case_id, user)
    with connect() as db:
        readiness = db.execute("SELECT * FROM pa_submission_readiness WHERE case_id=?", (case_id,)).fetchone()
        approval = db.execute("SELECT a.readiness_version,a.reason,a.approved_at,s.display_name FROM pa_submission_approvals a JOIN pa_staff s ON s.id=a.approved_by WHERE a.case_id=?", (case_id,)).fetchone()
        attempts = [dict(row) for row in db.execute("SELECT idempotency_key,adapter_type,status,reference_id,response_summary,attempted_at FROM pa_submission_attempts WHERE case_id=? ORDER BY id DESC", (case_id,))]
    if not readiness:
        return {"status":"DRAFT","readiness":None,"approval":None,"attempts":attempts}
    successful_demo = any(attempt["status"] == "SUBMITTED_DEMO" for attempt in attempts)
    status = "SUBMITTED_DEMO" if successful_demo else "APPROVED_FOR_SUBMISSION" if approval and approval["readiness_version"]==readiness["version"] else readiness["status"]
    return {"status":status,
            "readiness":{**json.loads(readiness["decision_json"]),"version":readiness["version"],"evaluated_at":readiness["evaluated_at"]},
            "approval":dict(approval) if approval else None,"attempts":attempts}


@router.post("/approve")
def approve_readiness(case_id: str, payload: ApprovalRequest, user: dict = Depends(current_user)):
    if user["role"] != "MANAGER":
        raise HTTPException(403, "Manager approval is required before demo submission")
    _require_scoped_case(case_id, user)
    with connect() as db:
        current = db.execute("SELECT * FROM pa_submission_readiness WHERE case_id=?", (case_id,)).fetchone()
        if not current or current["version"] != payload.readiness_version:
            raise HTTPException(409, "Refresh submission readiness before approval")
        latest = _load_decision(db, case_id, include_predictions=False)
        if latest["fingerprint"] != current["fingerprint"]:
            raise HTTPException(409, "Case data, documents, or requirements changed; rerun readiness before approval")
        if current["status"] != "READY_FOR_REVIEW":
            raise HTTPException(409, "Submission is not ready for manager approval")
        approved_at = _now()
        db.execute("INSERT INTO pa_submission_approvals(case_id,readiness_version,approved_by,reason,approved_at) VALUES(?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET readiness_version=excluded.readiness_version,approved_by=excluded.approved_by,reason=excluded.reason,approved_at=excluded.approved_at",
            (case_id, payload.readiness_version, user["id"], payload.reason.strip(), approved_at))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",
            (case_id, approved_at, user["display_name"], "SUBMISSION_APPROVED_FOR_DEMO", json.dumps({"readiness_version":payload.readiness_version,"reason":payload.reason.strip()}), "HUMAN_SUBMISSION_APPROVAL"))
    return {"case_id":case_id,"status":"APPROVED_FOR_SUBMISSION","approved_at":approved_at,"approved_by":user["display_name"]}


@router.post("/submit-demo")
def submit_demo(case_id: str, payload: DemoSubmitRequest, user: dict = Depends(current_user)):
    if user["role"] != "MANAGER":
        raise HTTPException(403, "Manager approval is required for demo submission")
    _require_scoped_case(case_id, user)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM pa_submission_attempts WHERE case_id=? AND idempotency_key=?", (case_id,payload.idempotency_key)).fetchone()
        if existing:
            return dict(existing)
        readiness = db.execute("SELECT * FROM pa_submission_readiness WHERE case_id=?", (case_id,)).fetchone()
        approval = db.execute("SELECT * FROM pa_submission_approvals WHERE case_id=?", (case_id,)).fetchone()
        if not readiness or readiness["version"] != payload.readiness_version or not approval or approval["readiness_version"] != payload.readiness_version:
            raise HTTPException(409, "A current manager approval is required before demo submission")
        latest = _load_decision(db, case_id, include_predictions=False)
        if latest["fingerprint"] != readiness["fingerprint"]:
            raise HTTPException(409, "Case data, documents, or requirements changed; rerun readiness and approval")
        if readiness["status"] != "READY_FOR_REVIEW":
            raise HTTPException(409, "Only a ready submission can be sent through the demo adapter")
        attempted_at = _now()
        case = dict(db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
        try:
            result = run_demo_adapter(case, json.loads(readiness["decision_json"]))
        except Exception as error:
            result = {"status":"FAILED","reference_id":None,"response_summary":f"Demo adapter failed ({type(error).__name__}); no payer was contacted."}
        cursor = db.execute("INSERT INTO pa_submission_attempts(case_id,idempotency_key,adapter_type,status,reference_id,response_summary,attempted_by,attempted_at) VALUES(?,?,?,?,?,?,?,?)",
            (case_id,payload.idempotency_key,"DEMO",result["status"],result["reference_id"],result["response_summary"],user["id"],attempted_at))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",
            (case_id,attempted_at,user["display_name"],"DEMO_SUBMISSION_RECORDED",json.dumps({"adapter":"DEMO","reference_id":result["reference_id"],"status":result["status"],"idempotency_key":payload.idempotency_key}),"SIMULATED_EXTERNAL_ACTION"))
        row = db.execute("SELECT * FROM pa_submission_attempts WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row)
