import json
from datetime import date, datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from database import connect
from rag import cosine_similarity, embed_text, explain_recovery_with_ollama


class RecoveryState(TypedDict, total=False):
    case_id: str
    error_code: str | None
    actor: str
    approve_recovery: bool
    preview_only: bool
    now: str
    case: dict
    events: list[dict]
    attempts: list[dict]
    policies: list[dict]
    retrieval_engine: str
    failure_type: str
    error_details: str
    policy_valid: bool
    validation_reason: str
    plan: dict
    result: dict
    explanation_engine: str


FAILURE_MAP = {
    "EVIDENCE_MISSING": "MISSING_EVIDENCE",
    "NOTIFY_TIMEOUT": "FAILED_NOTIFICATION",
    "INVALID_TRANSITION": "INVALID_STATUS_TRANSITION",
    "SERVICE_UNAVAILABLE": "UNAVAILABLE_SERVICE",
}


def detect_failure(state: RecoveryState) -> dict:
    with connect() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?", (state["case_id"],)).fetchone()
        if not case:
            raise ValueError("Case not found")
        events = [dict(row) for row in db.execute("SELECT * FROM workflow_failures WHERE case_id=? ORDER BY occurred_at DESC", (state["case_id"],))]
        attempts = [dict(row) for row in db.execute("SELECT * FROM recovery_attempts WHERE case_id=? ORDER BY created_at DESC", (state["case_id"],))]
    requested_code = state.get("error_code")
    event = next((item for item in events if not requested_code or item["error_code"] == requested_code), None)
    if not event:
        return {"case": dict(case), "events": events, "attempts": attempts, "failure_type": "UNKNOWN", "error_details": requested_code or "No failed workflow event was found."}
    return {"case": dict(case), "events": events, "attempts": attempts, "failure_type": FAILURE_MAP.get(event["error_code"], "UNKNOWN"), "error_details": event["error_details"], "error_code": event["error_code"]}


def retrieve_instructions(state: RecoveryState) -> dict:
    query = f"{state['failure_type']} {state.get('error_code', '')} {state['error_details']} recovery retry manual fallback"
    query_embedding, engine = embed_text(query)
    with connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM policy_documents WHERE approved=1 AND document_type IN ('TROUBLESHOOTING_GUIDE','RECOVERY_PROCEDURE','RETRY_POLICY','ERROR_DEFINITION','MANUAL_FALLBACK')")]
    scored: list[dict] = []
    for row in rows:
        stored = json.loads(row["embedding"]) if row.get("embedding") else None
        if stored is None or row.get("embedding_engine") != engine:
            stored, _ = embed_text(row["content"])
        row["similarity"] = round(cosine_similarity(query_embedding, stored), 4)
        row.pop("embedding", None)
        scored.append(row)
    scored.sort(key=lambda item: item["similarity"], reverse=True)
    return {"policies": scored, "retrieval_engine": engine}


def validate_instructions(state: RecoveryState) -> dict:
    today = date.fromisoformat(state["now"][:10])
    applicable = [item for item in state["policies"] if date.fromisoformat(item["effective_date"]) <= today and (not item["expires_at"] or date.fromisoformat(item["expires_at"]) >= today) and (not item.get("error_code") or item["error_code"] == state.get("error_code"))]
    required = {"RECOVERY_PROCEDURE", "RETRY_POLICY"}
    available = {item["document_type"] for item in applicable}
    if state["failure_type"] == "UNKNOWN":
        return {"policies": applicable, "policy_valid": False, "validation_reason": "The failure is not mapped to an approved error definition."}
    if not required.issubset(available):
        return {"policies": applicable, "policy_valid": False, "validation_reason": "No complete, current recovery procedure and retry policy applies."}
    versions: dict[str, set[str]] = {}
    for item in applicable:
        versions.setdefault(item["policy_id"], set()).add(item["version"])
    if any(len(values) > 1 for values in versions.values()):
        return {"policies": applicable, "policy_valid": False, "validation_reason": "Conflicting current recovery procedure versions were retrieved."}
    return {"policies": applicable, "policy_valid": True, "validation_reason": "Current approved recovery instructions apply."}


def plan_recovery(state: RecoveryState) -> dict:
    references = [{"policy_id": item["policy_id"], "document_type": item["document_type"], "effective_date": item["effective_date"], "version": item["version"], "similarity": item["similarity"]} for item in state["policies"]]
    if not state["policy_valid"]:
        return {"plan": {"likely_cause": state["validation_reason"], "recommended_recovery": "Stop automated recovery and request human intervention.", "retry_allowed": False, "human_approval_required": True, "source_references": references}}
    plans = {
        "MISSING_EVIDENCE": ("Required evidence was absent or unverified when the workflow action ran.", "Create a manual evidence follow-up task; do not retry until evidence is verified.", False, True),
        "FAILED_NOTIFICATION": ("The notification service did not confirm delivery.", "Retry the simulated notification once after confirming the recipient and service availability.", True, False),
        "INVALID_STATUS_TRANSITION": ("The requested status change is not permitted from the current case state.", "Route the proposed transition to an operations reviewer for correction.", False, True),
        "UNAVAILABLE_SERVICE": ("A required workflow service was unavailable.", "Retry the failed service action once after an availability check.", True, False),
    }
    cause, action, retry, approval = plans[state["failure_type"]]
    same_error = [item for item in state["attempts"] if item["error_code"] == state.get("error_code")]
    if len(same_error) >= 2 or any(item["recovery_status"] == "SUCCESS" for item in same_error):
        retry = False
        approval = True
        action = "Do not retry; create a manual follow-up for the responsible operations role."
        cause += " The bounded retry limit was reached or a successful recovery already exists."
    return {"plan": {"likely_cause": cause, "recommended_recovery": action, "retry_allowed": retry, "human_approval_required": approval, "source_references": references}}


def request_approval(state: RecoveryState) -> dict:
    plan = state["plan"]
    if state.get("preview_only", False):
        return {
            "result": {
                **plan,
                "human_approval_required": True,
                "recovery_status": "RECOMMENDATION_ONLY",
            }
        }
    if plan["human_approval_required"] and not state.get("approve_recovery", False):
        return {"result": {**plan, "recovery_status": "PENDING_APPROVAL"}}
    return {"result": {**plan, "recovery_status": "APPROVED"}}


def execute_recovery(state: RecoveryState) -> dict:
    result = state["result"]
    if result["recovery_status"] != "APPROVED":
        return {}
    if not result["retry_allowed"]:
        return {"result": {**result, "recovery_status": "MANUAL_FOLLOW_UP_CREATED"}}
    service_confirmed = state["failure_type"] in {"FAILED_NOTIFICATION", "UNAVAILABLE_SERVICE"}
    return {"result": {**result, "recovery_status": "EXECUTED" if service_confirmed else "FAILED"}}


def verify_result(state: RecoveryState) -> dict:
    result = state["result"]
    if result["recovery_status"] == "EXECUTED":
        return {"result": {**result, "recovery_status": "SUCCESS"}}
    if result["recovery_status"] == "FAILED":
        return {"result": {**result, "recovery_status": "MANUAL_FOLLOW_UP_CREATED", "recommended_recovery": "Recovery was not confirmed; a manual follow-up task was created."}}
    return {}


def explain_recovery(state: RecoveryState) -> dict:
    narrative, engine = explain_recovery_with_ollama(
        {"failure_type": state["failure_type"], **state["result"]}
    )
    return {
        "result": {**state["result"], "narrative": narrative},
        "explanation_engine": engine,
    }


def audit_recovery(state: RecoveryState) -> dict:
    result = {"case_id": state["case_id"], "failure_type": state["failure_type"], "error_details": state["error_details"], **state["result"]}
    with connect() as db:
        db.execute("INSERT INTO recovery_attempts(case_id,error_code,created_at,failure_type,recommended_recovery,retry_allowed,human_approval_required,recovery_status,source_references,actor) VALUES(?,?,?,?,?,?,?,?,?,?)", (state["case_id"], state.get("error_code") or "UNKNOWN", state["now"], state["failure_type"], result["recommended_recovery"], int(result["retry_allowed"]), int(result["human_approval_required"]), result["recovery_status"], json.dumps(result["source_references"]), state["actor"]))
        if result["recovery_status"] == "MANUAL_FOLLOW_UP_CREATED":
            db.execute("INSERT INTO manual_follow_ups(case_id,created_at,responsible_role,reason,status) VALUES(?,?,?,?,?)", (state["case_id"], state["now"], "Prior Authorization Supervisor", result["recommended_recovery"], "OPEN"))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)", (state["case_id"], state["now"], "Recovery RAG Agent", "RECOVERY_RAG_DECISION", json.dumps(result, sort_keys=True), "AGENT_RECOVERY_REQUIRES_CONFIRMATION"))
    return {"result": result}


builder = StateGraph(RecoveryState)
builder.add_node("failure_detection", detect_failure)
builder.add_node("retrieval", retrieve_instructions)
builder.add_node("validation", validate_instructions)
builder.add_node("recovery_planning", plan_recovery)
builder.add_node("approval", request_approval)
builder.add_node("execution", execute_recovery)
builder.add_node("verification", verify_result)
builder.add_node("explanation", explain_recovery)
builder.add_node("audit_logging", audit_recovery)
builder.add_edge(START, "failure_detection")
builder.add_edge("failure_detection", "retrieval")
builder.add_edge("retrieval", "validation")
builder.add_edge("validation", "recovery_planning")
builder.add_edge("recovery_planning", "approval")
builder.add_edge("approval", "execution")
builder.add_edge("execution", "verification")
builder.add_edge("verification", "explanation")
builder.add_edge("explanation", "audit_logging")
builder.add_edge("audit_logging", END)
recovery_rag_graph = builder.compile()


def run_recovery_rag(
    case_id: str,
    actor: str,
    error_code: str | None = None,
    approve_recovery: bool = False,
    preview_only: bool = False,
) -> dict:
    state = recovery_rag_graph.invoke(
        {
            "case_id": case_id,
            "actor": actor,
            "error_code": error_code,
            "approve_recovery": approve_recovery,
            "preview_only": preview_only,
            "now": datetime.now(timezone.utc).isoformat(),
        }
    )
    result = dict(state["result"])
    result["explanation_engine"] = state.get("explanation_engine", "not_run")
    return result
