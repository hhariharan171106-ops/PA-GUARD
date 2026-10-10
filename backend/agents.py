import json
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from database import connect
from rag import explain_with_ollama, retrieve_policies, source_references, validate_policies


class SlaRagState(TypedDict, total=False):
    case_id: str
    now: str
    case: dict
    documents: list[dict]
    task_history: list[dict]
    settings: dict
    policies: list[dict]
    retrieval_engine: str
    policy_valid: bool
    validation_reason: str
    result: dict
    explanation_engine: str


def load_case(state: SlaRagState) -> dict:
    with connect() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?", (state["case_id"],)).fetchone()
        if not case:
            raise ValueError("Case not found")
        documents = [dict(row) for row in db.execute("SELECT * FROM documents WHERE case_id=?", (state["case_id"],))]
        history = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE case_id=? ORDER BY timestamp DESC", (state["case_id"],))]
        settings = dict(db.execute("SELECT * FROM sla_settings WHERE id=1").fetchone())
    return {"case": dict(case), "documents": documents, "task_history": history, "settings": settings}


def retrieve(state: SlaRagState) -> dict:
    case = state["case"]
    query = f"SLA deadline rules for {case['status']} {case['priority']} prior authorization with evidence dependencies and escalation"
    policies, engine = retrieve_policies(query)
    return {"policies": policies, "retrieval_engine": engine}


def validate(state: SlaRagState) -> dict:
    valid, reason = validate_policies(state["policies"], datetime.fromisoformat(state["now"]).date())
    return {"policy_valid": valid, "validation_reason": reason}


def calculate_rules(state: SlaRagState) -> dict:
    case = state["case"]
    settings = state["settings"]
    now = datetime.fromisoformat(state["now"])
    deadline = datetime.fromisoformat(case["deadline"])
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    hours = round((deadline - now).total_seconds() / 3600, 1)
    gaps = [item for item in state["documents"] if item["status"] != "VERIFIED"]
    references = source_references(state["policies"])
    if not state["policy_valid"]:
        result = {"case_id": case["id"], "risk_level": "NEEDS_REVIEW", "remaining_time": f"{hours} hours", "remaining_hours": hours, "priority": "NEEDS_REVIEW", "explanation": state["validation_reason"], "recommended_action": "Route to an SLA manager to resolve policy coverage before prioritization.", "assigned_role": "SLA Manager", "source_references": references, "confidence": 0.0, "needs_human_review": True, "rule_reason": state["validation_reason"]}
        return {"result": result}
    if hours <= 0:
        risk, priority = "BREACHED", "HIGH"
    elif hours <= settings["urgent_hours"]:
        risk, priority = "HIGH", "HIGH"
    elif hours <= settings["warning_hours"] or gaps:
        risk, priority = "MEDIUM", "MEDIUM"
    else:
        risk, priority = "LOW", "LOW"
    dependency_text = f"{len(gaps)} unresolved evidence dependencies" if gaps else "no unresolved evidence dependencies"
    reason = f"The deadline has {hours} hours remaining and the case has {dependency_text}."
    action = "Escalate for human SLA manager review immediately." if risk == "BREACHED" else "Resolve missing or unverified evidence, then perform human review." if gaps else "Continue assigned workflow and monitor the deadline."
    role = "SLA Manager" if risk == "BREACHED" else "Evidence Coordinator" if gaps else "Prior Authorization Coordinator"
    confidence = round(min(0.98, 0.75 + 0.05 * len(references)), 2)
    result = {"case_id": case["id"], "risk_level": risk, "remaining_time": f"{hours} hours", "remaining_hours": hours, "priority": priority, "explanation": "", "recommended_action": action, "assigned_role": role, "source_references": references, "confidence": confidence, "needs_human_review": True, "rule_reason": reason}
    return {"result": result}


def explain(state: SlaRagState) -> dict:
    if not state["policy_valid"]:
        return {"explanation_engine": "not_run"}
    explanation, engine = explain_with_ollama(state["result"], state["policies"])
    result = {**state["result"], "explanation": explanation}
    return {"result": result, "explanation_engine": engine}


def audit_recommendation(state: SlaRagState) -> dict:
    result = state["result"]
    detail = json.dumps({"risk_level": result["risk_level"], "priority": result["priority"], "recommended_action": result["recommended_action"], "source_references": result["source_references"], "confidence": result["confidence"], "retrieval_engine": state["retrieval_engine"], "explanation_engine": state.get("explanation_engine", "not_run")}, sort_keys=True)
    with connect() as db:
        db.execute("INSERT INTO sla_recommendations(case_id,created_at,risk_level,remaining_hours,priority,explanation,recommended_action,assigned_role,source_references,confidence,needs_human_review) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (result["case_id"], state["now"], result["risk_level"], result["remaining_hours"], result["priority"], result["explanation"], result["recommended_action"], result["assigned_role"], json.dumps(result["source_references"]), result["confidence"], 1))
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)", (result["case_id"], state["now"], "SLA RAG Agent", "SLA_RAG_RECOMMENDATION", detail, "AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW"))
    result.pop("rule_reason", None)
    return {"result": result}


builder = StateGraph(SlaRagState)
builder.add_node("load_case", load_case)
builder.add_node("retrieval", retrieve)
builder.add_node("validation", validate)
builder.add_node("rule_calculation", calculate_rules)
builder.add_node("explanation", explain)
builder.add_node("audit_logging", audit_recommendation)
builder.add_edge(START, "load_case")
builder.add_edge("load_case", "retrieval")
builder.add_edge("retrieval", "validation")
builder.add_edge("validation", "rule_calculation")
builder.add_edge("rule_calculation", "explanation")
builder.add_edge("explanation", "audit_logging")
builder.add_edge("audit_logging", END)
sla_rag_graph = builder.compile()


def run_sla_rag(case_id: str, now: datetime | None = None) -> dict:
    calculation_time = now or datetime.now(timezone.utc)
    state = sla_rag_graph.invoke({"case_id": case_id, "now": calculation_time.isoformat()})
    return state["result"]


def run_sla_agent(case_ids: list[str]) -> list[dict]:
    recommendations = [run_sla_rag(case_id) for case_id in case_ids]
    order = {"BREACHED": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NEEDS_REVIEW": 4}
    return sorted(recommendations, key=lambda item: (order[item["risk_level"]], item["remaining_hours"]))
