import json
from datetime import date, datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from database import connect
from rag import embed_text, cosine_similarity, explain_evidence_with_ollama


class EvidenceState(TypedDict, total=False):
    case_id: str
    now: str
    case: dict
    documents: list[dict]
    policies: list[dict]
    retrieval_engine: str
    policy_valid: bool
    validation_reason: str
    result: dict
    explanation_engine: str


def load_case(state: EvidenceState) -> dict:
    with connect() as db:
        case = db.execute("SELECT * FROM cases WHERE id=?", (state["case_id"],)).fetchone()
        if not case:
            raise ValueError("Case not found")
        documents = [dict(row) for row in db.execute("SELECT * FROM documents WHERE case_id=? ORDER BY id", (state["case_id"],))]
    return {"case": dict(case), "documents": documents}


def retrieve_requirements(state: EvidenceState) -> dict:
    case = state["case"]
    query = f"{case['payer']} {case['service']} required documents clinical documentation checklist authorization guideline"
    query_embedding, engine = embed_text(query)
    with connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM policy_documents WHERE approved=1 AND document_type IN ('PAYER_REQUIREMENT','DOCUMENT_CHECKLIST','CLINICAL_DOCUMENTATION_RULE','AUTHORIZATION_GUIDELINE') AND (payer IS NULL OR payer=? OR payer='ALL') AND (service_type IS NULL OR service_type=? OR service_type='ALL')", (case["payer"], case["service"]))]
    scored: list[dict] = []
    for row in rows:
        stored = json.loads(row["embedding"]) if row.get("embedding") else None
        if stored is None or row.get("embedding_engine") != engine:
            stored, _ = embed_text(row["content"])
        row["similarity"] = round(cosine_similarity(query_embedding, stored), 4)
        row.pop("embedding", None)
        scored.append(row)
    return {"policies": sorted(scored, key=lambda item: item["similarity"], reverse=True)[:6], "retrieval_engine": engine}


def validate_requirements(state: EvidenceState) -> dict:
    policies = state["policies"]
    as_of = date.fromisoformat(state["now"][:10])
    if not policies:
        return {"policy_valid": False, "validation_reason": "No approved payer requirements were retrieved for this payer and service."}
    current = [item for item in policies if date.fromisoformat(item["effective_date"]) <= as_of and (not item["expires_at"] or date.fromisoformat(item["expires_at"]) >= as_of)]
    if not current:
        return {"policy_valid": False, "validation_reason": "Retrieved payer requirements are not effective on the review date."}
    versions: dict[str, set[str]] = {}
    for item in current:
        versions.setdefault(item["policy_id"], set()).add(item["version"])
    conflicts = [policy_id for policy_id, values in versions.items() if len(values) > 1]
    if conflicts:
        return {"policy_valid": False, "validation_reason": f"Conflicting current requirement versions: {', '.join(conflicts)}."}
    return {"policy_valid": True, "validation_reason": "Approved payer requirements are current and internally consistent.", "policies": current}


def compare_documents(state: EvidenceState) -> dict:
    case = state["case"]
    references = [{"policy_id": item["policy_id"], "document_type": item["document_type"], "effective_date": item["effective_date"], "version": item["version"], "payer": item.get("payer"), "service_type": item.get("service_type"), "source_reference": item.get("source_reference"), "similarity": item["similarity"]} for item in state["policies"]]
    if not state["policy_valid"]:
        return {"result": {"case_id": case["id"], "required_documents": [], "missing_documents": [], "possible_mismatches": [state["validation_reason"]], "verification_status": "NEEDS_REVIEW", "explanation": state["validation_reason"], "recommended_action": "Route to a human evidence reviewer to resolve requirement coverage.", "source_references": references, "needs_human_review": True}}
    required: list[str] = []
    for policy in state["policies"]:
        configured = json.loads(policy.get("required_documents") or "[]")
        required.extend(str(item) for item in configured)
    required = list(dict.fromkeys(required))
    by_name = {item["name"].strip().lower(): item for item in state["documents"]}
    missing = [name for name in required if name.strip().lower() not in by_name or by_name[name.strip().lower()]["status"] == "MISSING"]
    mismatches = [f"{item['name']} is marked {item['status']}; metadata does not establish content validity." for item in state["documents"] if item["status"] in ("RECEIVED", "NEEDS REVIEW")]
    unresolved = missing or mismatches
    status = "NEEDS_REVIEW" if unresolved else "VERIFIED"
    action = f"Request {missing[0]} from the care team." if missing else "Ask a qualified human reviewer to verify received evidence." if mismatches else "No document action is recommended; retain human oversight."
    return {"result": {"case_id": case["id"], "required_documents": required, "missing_documents": missing, "possible_mismatches": mismatches, "verification_status": status, "explanation": "", "recommended_action": action, "source_references": references, "needs_human_review": True}}


def explain_finding(state: EvidenceState) -> dict:
    if not state["policy_valid"]:
        return {"explanation_engine": "not_run"}
    explanation, engine = explain_evidence_with_ollama(state["result"], state["policies"])
    return {"result": {**state["result"], "explanation": explanation}, "explanation_engine": engine}


def audit_finding(state: EvidenceState) -> dict:
    result = state["result"]
    now = state["now"]
    with connect() as db:
        db.execute("INSERT INTO evidence_recommendations(case_id,created_at,verification_status,required_documents,missing_documents,possible_mismatches,explanation,recommended_action,source_references,needs_human_review) VALUES(?,?,?,?,?,?,?,?,?,?)", (result["case_id"], now, result["verification_status"], json.dumps(result["required_documents"]), json.dumps(result["missing_documents"]), json.dumps(result["possible_mismatches"]), result["explanation"], result["recommended_action"], json.dumps(result["source_references"]), 1))
        detail = json.dumps({"verification_status": result["verification_status"], "missing_documents": result["missing_documents"], "possible_mismatches": result["possible_mismatches"], "recommended_action": result["recommended_action"], "source_references": result["source_references"], "retrieval_engine": state["retrieval_engine"], "explanation_engine": state.get("explanation_engine", "not_run")}, sort_keys=True)
        db.execute("INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)", (result["case_id"], now, "Evidence RAG Agent", "EVIDENCE_RAG_FINDING", detail, "AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW"))
    return {"result": result}


builder = StateGraph(EvidenceState)
builder.add_node("case_retrieval", load_case)
builder.add_node("policy_retrieval", retrieve_requirements)
builder.add_node("validation", validate_requirements)
builder.add_node("comparison", compare_documents)
builder.add_node("recommendation", explain_finding)
builder.add_node("audit_logging", audit_finding)
builder.add_edge(START, "case_retrieval")
builder.add_edge("case_retrieval", "policy_retrieval")
builder.add_edge("policy_retrieval", "validation")
builder.add_edge("validation", "comparison")
builder.add_edge("comparison", "recommendation")
builder.add_edge("recommendation", "audit_logging")
builder.add_edge("audit_logging", END)
evidence_rag_graph = builder.compile()


def run_evidence_rag(case_id: str, now: datetime | None = None) -> dict:
    state = evidence_rag_graph.invoke({"case_id": case_id, "now": (now or datetime.now(timezone.utc)).isoformat()})
    return state["result"]
