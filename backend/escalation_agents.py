import json
from datetime import date, datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from database import connect
from rag import cosine_similarity, embed_text, explain_escalation_with_ollama


class EscalationState(TypedDict, total=False):
    case_id: str
    now: str
    actor: str
    approve_notification: bool
    case: dict
    attempts: list[dict]
    settings: dict
    policies: list[dict]
    retrieval_engine: str
    policy_valid: bool
    validation_reason: str
    result: dict
    explanation_engine: str


def load_context(state: EscalationState) -> dict:
    with connect() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?", (state["case_id"],)).fetchone()
        if not case:
            raise ValueError("Case not found")
        attempts = [dict(row) for row in db.execute("SELECT * FROM escalation_recommendations WHERE case_id=? ORDER BY created_at DESC", (state["case_id"],))]
        settings = dict(db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone())
    return {"case": dict(case), "attempts": attempts, "settings": settings}


def retrieve_policy(state: EscalationState) -> dict:
    case = state["case"]
    query = f"escalation policy role responsibility notification procedure warning threshold prior authorization department {case['status']} {case['risk']}"
    query_embedding, engine = embed_text(query)
    with connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM policy_documents WHERE approved=1 AND document_type IN ('ESCALATION_RULE','ROLE_RESPONSIBILITY','NOTIFICATION_PROCEDURE','SLA_POLICY')")]
    scored: list[dict] = []
    for row in rows:
        stored = json.loads(row["embedding"]) if row.get("embedding") else None
        if stored is None or row.get("embedding_engine") != engine:
            stored, _ = embed_text(row["content"])
        row["similarity"] = round(cosine_similarity(query_embedding, stored), 4)
        row.pop("embedding", None)
        scored.append(row)
    return {"policies": sorted(scored, key=lambda item: item["similarity"], reverse=True)[:6], "retrieval_engine": engine}


def validate_policy(state: EscalationState) -> dict:
    as_of = date.fromisoformat(state["now"][:10])
    policies = state["policies"]
    current = [item for item in policies if date.fromisoformat(item["effective_date"]) <= as_of and (not item["expires_at"] or date.fromisoformat(item["expires_at"]) >= as_of) and (not item.get("department") or item["department"] in ("All departments", "Prior Authorization"))]
    if not current:
        return {"policy_valid": False, "validation_reason": "No current approved escalation policy applies to this case."}
    versions: dict[str, set[str]] = {}
    for item in current:
        versions.setdefault(item["policy_id"], set()).add(item["version"])
    conflicts = [policy_id for policy_id, values in versions.items() if len(values) > 1]
    if conflicts:
        return {"policy_valid": False, "validation_reason": f"Conflicting current escalation policy versions: {', '.join(conflicts)}."}
    if not any(item["document_type"] == "ESCALATION_RULE" for item in current):
        return {"policy_valid": False, "validation_reason": "A current approved escalation rule was not retrieved."}
    return {"policy_valid": True, "validation_reason": "Current approved escalation policy applies.", "policies": current}


def evaluate_rules(state: EscalationState) -> dict:
    case = state["case"]
    now = datetime.fromisoformat(state["now"])
    deadline = datetime.fromisoformat(case["deadline"])
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining_hours = round((deadline - now).total_seconds() / 3600, 1)
    references = [{"policy_id": item["policy_id"], "document_type": item["document_type"], "effective_date": item["effective_date"], "version": item["version"], "similarity": item["similarity"]} for item in state["policies"]]
    if not state["policy_valid"]:
        return {"result": {"case_id": case["id"], "escalation_required": False, "escalation_level": "NEEDS_REVIEW", "reason": state["validation_reason"], "responsible_role": "SLA Manager", "recommended_action": "Resolve escalation policy coverage with a human manager.", "approval_required": True, "source_references": references, "notification_status": "NOT_SENT", "remaining_hours": remaining_hours, "duplicate_notification": False}}
    settings = state["settings"]
    if remaining_hours <= settings["escalation_two_hours"]:
        required, level, role = True, "LEVEL_2", "SLA Manager"
    elif remaining_hours <= settings["escalation_one_hours"]:
        required, level, role = True, "LEVEL_1", "Prior Authorization Supervisor"
    else:
        required, level, role = False, "NONE", "Prior Authorization Coordinator"
    duplicate = any(item["escalation_level"] == level and item["notification_status"] in ("SIMULATED_DELIVERED", "PENDING_APPROVAL") for item in state["attempts"])
    failed = any(item["notification_status"] == "SIMULATED_FAILED" for item in state["attempts"])
    reason = f"{remaining_hours} hours remain; configured thresholds are level 1 at {settings['escalation_one_hours']} hours and level 2 at {settings['escalation_two_hours']} hours."
    action = "Review the prior failed attempt and confirm the recipient before retrying." if failed else f"Escalate to the {role}." if required else "Continue monitoring; no escalation threshold has been reached."
    return {"result": {"case_id": case["id"], "escalation_required": required, "escalation_level": level, "reason": reason, "responsible_role": role, "recommended_action": action, "approval_required": required, "source_references": references, "notification_status": "DUPLICATE_SUPPRESSED" if duplicate else "PENDING_APPROVAL" if required else "NOT_REQUIRED", "remaining_hours": remaining_hours, "duplicate_notification": duplicate}}


def approval(state: EscalationState) -> dict:
    result = state["result"]
    if result["notification_status"] != "PENDING_APPROVAL":
        return {}
    if not state.get("approve_notification", False):
        return {}
    return {"result": {**result, "notification_status": "APPROVED_FOR_SIMULATION"}}


def simulate_notification(state: EscalationState) -> dict:
    result = state["result"]
    if result["notification_status"] != "APPROVED_FOR_SIMULATION":
        return {}
    return {"result": {**result, "notification_status": "SIMULATED_DELIVERED"}}


def explain_decision(state: EscalationState) -> dict:
    result = state["result"]
    if not state["policy_valid"]:
        return {"explanation_engine": "not_run"}
    explanation, engine = explain_escalation_with_ollama(result, state["policies"])
    return {"result": {**result, "reason": explanation}, "explanation_engine": engine}


def audit_decision(state: EscalationState) -> dict:
    result = state["result"]
    with connect() as db:
        db.execute("INSERT INTO escalation_recommendations(case_id,created_at,escalation_required,escalation_level,reason,responsible_role,recommended_action,approval_required,source_references,notification_status,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (result["case_id"], state["now"], int(result["escalation_required"]), result["escalation_level"], result["reason"], result["responsible_role"], result["recommended_action"], int(result["approval_required"]), json.dumps(result["source_references"]), result["notification_status"], state["actor"]))
        detail = json.dumps({key: result[key] for key in ("escalation_required", "escalation_level", "responsible_role", "recommended_action", "approval_required", "notification_status", "source_references")}, sort_keys=True)
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)", (result["case_id"], state["now"], "Escalation RAG Agent", "ESCALATION_RAG_DECISION", detail, "AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW"))
    result.pop("remaining_hours", None)
    result.pop("duplicate_notification", None)
    return {"result": result}


builder = StateGraph(EscalationState)
builder.add_node("case_retrieval", load_context)
builder.add_node("policy_retrieval", retrieve_policy)
builder.add_node("policy_validation", validate_policy)
builder.add_node("rule_evaluation", evaluate_rules)
builder.add_node("approval", approval)
builder.add_node("notification_simulation", simulate_notification)
builder.add_node("explanation", explain_decision)
builder.add_node("audit_logging", audit_decision)
builder.add_edge(START, "case_retrieval")
builder.add_edge("case_retrieval", "policy_retrieval")
builder.add_edge("policy_retrieval", "policy_validation")
builder.add_edge("policy_validation", "rule_evaluation")
builder.add_edge("rule_evaluation", "approval")
builder.add_edge("approval", "notification_simulation")
builder.add_edge("notification_simulation", "explanation")
builder.add_edge("explanation", "audit_logging")
builder.add_edge("audit_logging", END)
escalation_rag_graph = builder.compile()


def run_escalation_rag(case_id: str, actor: str, approve_notification: bool = False, now: datetime | None = None) -> dict:
    state = escalation_rag_graph.invoke({"case_id": case_id, "actor": actor, "approve_notification": approve_notification, "now": (now or datetime.now(timezone.utc)).isoformat()})
    return state["result"]
