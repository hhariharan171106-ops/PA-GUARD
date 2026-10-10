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
    level_roles: dict[str, str]
    result: dict
    explanation_engine: str


def load_context(state: EscalationState) -> dict:
    with connect() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?", (state["case_id"],)).fetchone()
        if not case:
            raise ValueError("Case not found")
        attempts = [dict(row) for row in db.execute("SELECT * FROM escalation_recommendations WHERE case_id=? ORDER BY created_at DESC", (state["case_id"],))]
        settings_row = db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone()
        if not settings_row:
            raise ValueError("SLA escalation settings are unavailable")
    return {"case": dict(case), "attempts": attempts, "settings": dict(settings_row)}


def retrieve_policy(state: EscalationState) -> dict:
    case = state["case"]
    query = f"escalation thresholds levels responsible roles notification approval department Prior Authorization {case['status']} {case['risk']}"
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
    scored.sort(key=lambda item: item["similarity"], reverse=True)
    return {"policies": scored, "retrieval_engine": engine}


def validate_policy(state: EscalationState) -> dict:
    as_of = date.fromisoformat(state["now"][:10])
    applicable = []
    for item in state["policies"]:
        current = date.fromisoformat(item["effective_date"]) <= as_of and (not item["expires_at"] or date.fromisoformat(item["expires_at"]) >= as_of)
        department = item.get("department")
        if current and (not department or department in ("All departments", "Prior Authorization")):
            applicable.append(item)
    if not applicable:
        return {"policy_valid": False, "validation_reason": "No current approved escalation policy applies to this case.", "level_roles": {}}
    required_types = {"ESCALATION_RULE", "ROLE_RESPONSIBILITY", "NOTIFICATION_PROCEDURE", "SLA_POLICY"}
    available_types = {item["document_type"] for item in applicable}
    missing = required_types - available_types
    if missing:
        return {"policy_valid": False, "validation_reason": f"Missing required policy types: {', '.join(sorted(missing))}.", "policies": applicable, "level_roles": {}}
    by_id: dict[str, set[str]] = {}
    for item in applicable:
        by_id.setdefault(item["policy_id"], set()).add(item["version"])
    conflicts = [policy_id for policy_id, versions in by_id.items() if len(versions) > 1]
    if conflicts:
        return {"policy_valid": False, "validation_reason": f"Conflicting current policy versions: {', '.join(conflicts)}.", "policies": applicable, "level_roles": {}}
    with connect() as db:
        role_rows = [dict(row) for row in db.execute("SELECT escalation_level,responsible_role FROM escalation_role_assignments WHERE department IN ('All departments','Prior Authorization') AND effective_date<=? AND (expires_at IS NULL OR expires_at>=?)", (as_of.isoformat(), as_of.isoformat()))]
    level_roles: dict[str, str] = {}
    for row in role_rows:
        level = row["escalation_level"]
        role = row["responsible_role"]
        if level in level_roles and level_roles[level] != role:
            return {"policy_valid": False, "validation_reason": f"Conflicting recipients are configured for {level}.", "policies": applicable, "level_roles": {}}
        level_roles[level] = role
    if not all(level in level_roles for level in ("LEVEL_1", "LEVEL_2")):
        return {"policy_valid": False, "validation_reason": "Responsible roles are not configured for every escalation level.", "policies": applicable, "level_roles": {}}
    return {"policy_valid": True, "validation_reason": "Current approved escalation policy applies.", "policies": applicable, "level_roles": level_roles}


def evaluate_rules(state: EscalationState) -> dict:
    case = state["case"]
    now = datetime.fromisoformat(state["now"])
    deadline = datetime.fromisoformat(case["deadline"])
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining_hours = round((deadline - now).total_seconds() / 3600, 1)
    references = [{"policy_id": item["policy_id"], "document_type": item["document_type"], "effective_date": item["effective_date"], "version": item["version"], "similarity": item["similarity"]} for item in state["policies"]]
    if not state["policy_valid"]:
        return {"result": {"case_id": case["id"], "escalation_required": False, "escalation_level": "NEEDS_REVIEW", "reason": state["validation_reason"], "responsible_role": "Human policy reviewer", "recommended_action": "Resolve policy coverage or conflicts before sending any notification.", "approval_required": True, "source_references": references, "notification_status": "NOT_SENT", "remaining_hours": remaining_hours}}
    settings = state["settings"]
    if remaining_hours <= settings["escalation_two_hours"]:
        required, level = True, "LEVEL_2"
    elif remaining_hours <= settings["escalation_one_hours"]:
        required, level = True, "LEVEL_1"
    else:
        required, level = False, "NONE"
    role = state["level_roles"].get(level, case["coordinator"] if level == "NONE" else "Human policy reviewer")
    same_level = [item for item in state["attempts"] if item["escalation_level"] == level]
    duplicate = any(item["notification_status"] in ("SIMULATED_DELIVERED", "PENDING_APPROVAL") for item in same_level)
    failed = any(item["notification_status"] == "SIMULATED_FAILED" for item in same_level)
    reason = f"{remaining_hours} hours remain; configured thresholds are level 1 at {settings['escalation_one_hours']} hours and level 2 at {settings['escalation_two_hours']} hours."
    if duplicate:
        action = "Do not send another alert; review the existing notification record."
    elif failed:
        action = "Review the previous failed simulation and confirm the configured role before retrying."
    elif required:
        action = f"Request human approval to simulate notification to {role}."
    else:
        action = "Continue monitoring; no escalation threshold has been reached."
    status = "DUPLICATE_SUPPRESSED" if duplicate else "PENDING_APPROVAL" if required else "NOT_REQUIRED"
    return {"result": {"case_id": case["id"], "escalation_required": required, "escalation_level": level, "reason": reason, "responsible_role": role, "recommended_action": action, "approval_required": required, "source_references": references, "notification_status": status, "remaining_hours": remaining_hours}}


def approval(state: EscalationState) -> dict:
    result = state["result"]
    if result["notification_status"] != "PENDING_APPROVAL" or not state.get("approve_notification", False):
        return {}
    return {"result": {**result, "notification_status": "APPROVED_FOR_SIMULATION"}}


def simulate_notification(state: EscalationState) -> dict:
    result = state["result"]
    if result["notification_status"] != "APPROVED_FOR_SIMULATION":
        return {}
    confirmed = bool(result["responsible_role"] and result["responsible_role"] != "Human policy reviewer")
    status = "SIMULATED_DELIVERED" if confirmed else "SIMULATED_FAILED"
    action = f"Simulated notification confirmed for {result['responsible_role']}." if confirmed else "Simulation failed; request human review of the configured recipient."
    return {"result": {**result, "notification_status": status, "recommended_action": action}}


def explain_decision(state: EscalationState) -> dict:
    if not state["policy_valid"]:
        return {"explanation_engine": "not_run"}
    explanation, engine = explain_escalation_with_ollama(state["result"], state["policies"])
    return {"result": {**state["result"], "reason": explanation}, "explanation_engine": engine}


def audit_decision(state: EscalationState) -> dict:
    result = state["result"]
    with connect() as db:
        db.execute("INSERT INTO escalation_recommendations(case_id,created_at,escalation_required,escalation_level,reason,responsible_role,recommended_action,approval_required,source_references,notification_status,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (result["case_id"], state["now"], int(result["escalation_required"]), result["escalation_level"], result["reason"], result["responsible_role"], result["recommended_action"], int(result["approval_required"]), json.dumps(result["source_references"]), result["notification_status"], state["actor"]))
        detail = json.dumps({key: result[key] for key in ("escalation_required", "escalation_level", "responsible_role", "recommended_action", "approval_required", "notification_status", "source_references")}, sort_keys=True)
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)", (result["case_id"], state["now"], "Escalation RAG Agent", "ESCALATION_RAG_DECISION", detail, "AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW"))
    result.pop("remaining_hours", None)
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