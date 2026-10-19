import json
import re
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


def _text_tokens(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"[a-z0-9]+", text.casefold()))


def _document_excerpt(text: str, start: int, end: int) -> str:
    excerpt_start = max(0, start - 70)
    excerpt_end = min(len(text), end + 110)
    excerpt = " ".join(text[excerpt_start:excerpt_end].split())
    if excerpt_start:
        excerpt = f"...{excerpt}"
    if excerpt_end < len(text):
        excerpt = f"{excerpt}..."
    return excerpt


def _evidence_match(text: str, required_document: str) -> tuple[str | None, str | None]:
    source_tokens = _text_tokens(text)
    required_tokens = [token.group() for token in _text_tokens(required_document)]
    if not source_tokens or not required_tokens:
        return None, None
    source_values = [token.group() for token in source_tokens]
    for index in range(len(source_values) - len(required_tokens) + 1):
        if source_values[index:index + len(required_tokens)] == required_tokens:
            first = source_tokens[index]
            last = source_tokens[index + len(required_tokens) - 1]
            return "FOUND_IN_TEXT", _document_excerpt(text, first.start(), last.end())

    matched = set(source_values).intersection(required_tokens)
    minimum_matches = max(1, (len(required_tokens) + 1) // 2)
    if len(required_tokens) > 1 and len(matched) >= minimum_matches:
        matching_index = next(
            index for index, token in enumerate(source_values) if token in matched
        )
        token = source_tokens[matching_index]
        return (
            "POSSIBLE_MATCH",
            _document_excerpt(text, token.start(), token.end()),
        )
    return None, None


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
        return {"result": {"case_id": case["id"], "required_documents": [], "found_documents": [], "possible_evidence_matches": [], "missing_documents": [], "unreadable_documents": [], "possible_mismatches": [state["validation_reason"]], "verification_status": "NEEDS_REVIEW", "explanation": state["validation_reason"], "recommended_action": "Route to a human evidence reviewer to resolve requirement coverage.", "source_references": references, "needs_human_review": True}}
    required: list[str] = []
    for policy in state["policies"]:
        configured = json.loads(policy.get("required_documents") or "[]")
        required.extend(str(item) for item in configured)
    required = list(dict.fromkeys(required))
    found: list[dict] = []
    possible_matches: list[dict] = []
    unreadable: list[dict] = []
    mismatches: list[str] = []
    processed_documents: list[dict] = []
    for document in state["documents"]:
        if document["status"] == "MISSING":
            continue
        document_name = document.get("original_filename") or document["name"]
        extracted_text = (document.get("extracted_text") or "").strip()
        if document.get("ocr_status") != "COMPLETED" or not extracted_text:
            reason = document.get("processing_error") or (
                "OCR has not completed for this document."
                if document.get("ocr_status") != "FAILED"
                else "OCR failed without producing readable text."
            )
            unreadable.append({"document_name": document_name, "reason": reason})
            continue

        try:
            pages = json.loads(document.get("page_text") or "[]")
        except (TypeError, json.JSONDecodeError):
            pages = []
        if not isinstance(pages, list) or not pages:
            pages = [extracted_text]
        normalized_pages = [str(page or "") for page in pages]
        if len(re.sub(r"\s+", " ", extracted_text).strip()) < 20:
            unreadable.append(
                {
                    "document_name": document_name,
                    "reason": "OCR text is too short to reliably assess evidence.",
                }
            )
        else:
            processed_documents.append(document)

        for required_document in required:
            for page_number, page in enumerate(normalized_pages, start=1):
                match_status, excerpt = _evidence_match(page, required_document)
                if not match_status:
                    continue
                match = {
                    "required_document": required_document,
                    "document_name": document_name,
                    "page_number": page_number,
                    "excerpt": excerpt,
                }
                (found if match_status == "FOUND_IN_TEXT" else possible_matches).append(match)
                category = (document.get("category") or "").strip().casefold()
                if category and category != required_document.strip().casefold():
                    mismatches.append(
                        f"{document_name} is categorized as '{document['category']}', "
                        f"but its OCR text may relate to '{required_document}'; verify the document type."
                    )

    found_requirements = {item["required_document"] for item in found}
    possible_requirements = {item["required_document"] for item in possible_matches}
    can_identify_not_found = bool(processed_documents) and not unreadable
    missing = [
        name
        for name in required
        if can_identify_not_found
        and name not in found_requirements
        and name not in possible_requirements
    ]
    if unreadable:
        action = "Resolve unreadable or unprocessed documents, then rerun the Evidence Agent; OCR failure is not evidence of absence."
    elif missing:
        action = f"Request {missing[0]} from the care team for human review."
    else:
        action = "Ask a qualified human reviewer to verify each text match and requirement; no authorization decision is made."
    return {"result": {
        "case_id": case["id"],
        "required_documents": required,
        "found_documents": found,
        "possible_evidence_matches": possible_matches,
        "missing_documents": missing,
        "unreadable_documents": unreadable,
        "possible_mismatches": list(dict.fromkeys(mismatches)),
        "verification_status": "NEEDS_REVIEW",
        "explanation": "",
        "recommended_action": action,
        "source_references": references,
        "needs_human_review": True,
    }}


def explain_finding(state: EvidenceState) -> dict:
    if not state["policy_valid"]:
        return {"explanation_engine": "not_run"}
    explanation, engine = explain_evidence_with_ollama(state["result"])
    return {"result": {**state["result"], "explanation": explanation}, "explanation_engine": engine}


def audit_finding(state: EvidenceState) -> dict:
    result = state["result"]
    now = state["now"]
    with connect() as db:
        db.execute("INSERT INTO evidence_recommendations(case_id,created_at,verification_status,required_documents,missing_documents,possible_mismatches,explanation,recommended_action,source_references,needs_human_review) VALUES(?,?,?,?,?,?,?,?,?,?)", (result["case_id"], now, result["verification_status"], json.dumps(result["required_documents"]), json.dumps(result["missing_documents"]), json.dumps(result["possible_mismatches"]), result["explanation"], result["recommended_action"], json.dumps(result["source_references"]), 1))
        detail = json.dumps({"verification_status": result["verification_status"], "found_documents": result["found_documents"], "possible_evidence_matches": result["possible_evidence_matches"], "missing_documents": result["missing_documents"], "unreadable_documents": result["unreadable_documents"], "possible_mismatches": result["possible_mismatches"], "recommended_action": result["recommended_action"], "source_references": result["source_references"], "retrieval_engine": state["retrieval_engine"], "explanation_engine": state.get("explanation_engine", "not_run")}, sort_keys=True)
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
    result = dict(state["result"])
    result["explanation_engine"] = state.get("explanation_engine", "not_run")
    return result
