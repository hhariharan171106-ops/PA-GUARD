import json
import re
from datetime import date, datetime, timezone
from typing import TypedDict
from urllib.parse import urlparse

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
    payer_clean = case["payer"].strip()
    service_clean = case["service"].strip()
    query = f"{payer_clean} {service_clean} required documents clinical documentation checklist authorization guideline"
    query_embedding, engine = embed_text(query)
    with connect() as db:
        rows = [
            dict(row)
            for row in db.execute(
                """SELECT * FROM policy_documents
                   WHERE approved=1
                     AND document_type IN (
                         'PAYER_REQUIREMENT',
                         'DOCUMENT_CHECKLIST',
                         'CLINICAL_DOCUMENTATION_RULE',
                         'AUTHORIZATION_GUIDELINE'
                     )
                     AND (payer IS NULL OR LOWER(TRIM(payer)) = LOWER(?) OR UPPER(TRIM(payer))='ALL')
                     AND (service_type IS NULL OR LOWER(TRIM(service_type)) = LOWER(?) OR UPPER(TRIM(service_type))='ALL')""",
                (payer_clean, service_clean),
            )
        ]
    scored: list[dict] = []
    for row in rows:
        stored = json.loads(row["embedding"]) if row.get("embedding") else None
        if stored is None or row.get("embedding_engine") != engine:
            stored, _ = embed_text(row["content"])
        row["similarity"] = round(cosine_similarity(query_embedding, stored), 4)
        row.pop("embedding", None)
        scored.append(row)
    # Prefer the most specific payer/service scope. Semantic similarity ranks
    # candidates only within that scope and cannot displace an exact policy.
    for item in scored:
        item["scope_specificity"] = int(bool(item.get("payer"))) + int(bool(item.get("service_type")))
    if scored:
        best_scope = max(item["scope_specificity"] for item in scored)
        scored = [item for item in scored if item["scope_specificity"] == best_scope]
    return {
        "policies": sorted(scored, key=lambda item: item["similarity"], reverse=True),
        "retrieval_engine": engine,
    }


def validate_requirements(state: EvidenceState) -> dict:
    policies = state["policies"]
    as_of = date.fromisoformat(state["now"][:10])
    if not policies:
        return {
            "policy_valid": False,
            "policy_mode": "UNAVAILABLE",
            "validation_reason": "No approved payer requirements were retrieved for this payer and service.",
        }

    unsubstantiated_public = [
        item for item in policies
        if item.get("verification_status") == "VERIFIED_PUBLIC"
        and not all(item.get(field) for field in (
            "source_url", "authoritative_title", "authoritative_version",
            "source_verified_by", "source_verified_at", "source_verification_note",
        ))
        or item.get("verification_status") == "VERIFIED_PUBLIC"
        and (
            urlparse(item.get("source_url") or "").scheme != "https"
            or not (
                (urlparse(item.get("source_url") or "").hostname or "").lower() == "cms.gov"
                or (urlparse(item.get("source_url") or "").hostname or "").lower().endswith(".cms.gov")
            )
            or item.get("source_url") != item.get("source_reference")
        )
        or item.get("verification_status") == "VERIFIED_PUBLIC"
        and not _has_requirement_level_source_support(item)
    ]
    if unsubstantiated_public:
        labels = ", ".join(f"{item['policy_id']} v{item['version']}" for item in unsubstantiated_public)
        return {
            "policy_valid": False,
            "policy_mode": "UNAVAILABLE",
            "validation_reason": f"Public policy verification metadata is incomplete for {labels}; route to a policy reviewer.",
        }

    # Inspect policy effective dates
    effective_policies = [
        item for item in policies if date.fromisoformat(item["effective_date"]) <= as_of
    ]
    if not effective_policies:
        earliest_future = min(date.fromisoformat(item["effective_date"]) for item in policies)
        return {
            "policy_valid": False,
            "policy_mode": "UNAVAILABLE",
            "validation_reason": (
                f"Retrieved payer requirements are not yet effective on review date {as_of.isoformat()} "
                f"(effective from {earliest_future.isoformat()})."
            ),
        }

    current = [
        item
        for item in effective_policies
        if not item.get("expires_at") or date.fromisoformat(item["expires_at"]) >= as_of
    ]
    if not current:
        latest_expired = max(
            date.fromisoformat(item["expires_at"])
            for item in effective_policies
            if item.get("expires_at")
        )
        return {
            "policy_valid": False,
            "policy_mode": "UNAVAILABLE",
            "validation_reason": (
                f"Retrieved payer requirements are expired as of review date {as_of.isoformat()} "
                f"(expired on {latest_expired.isoformat()})."
            ),
        }

    versions: dict[str, set[str]] = {}
    for item in current:
        versions.setdefault(item["policy_id"], set()).add(item["version"])
    conflicts = [policy_id for policy_id, values in versions.items() if len(values) > 1]
    if conflicts:
        return {
            "policy_valid": False,
            "policy_mode": "UNAVAILABLE",
            "validation_reason": f"Conflicting current requirement versions: {', '.join(conflicts)}.",
        }
    policy_mode = (
        "AUTHORITATIVE"
        if all(item.get("verification_status") == "VERIFIED_PUBLIC" for item in current)
        else "DEMO_ONLY"
    )
    return {
        "policy_valid": True,
        "policy_mode": policy_mode,
        "validation_reason": (
            "Verified public payer requirements are current and internally consistent."
            if policy_mode == "AUTHORITATIVE"
            else "Approved demo requirements are available for demonstration only; they are not authoritative payer guidance."
        ),
        "policies": current,
    }


def _has_requirement_level_source_support(policy: dict) -> bool:
    try:
        support = json.loads(policy.get("source_requirement_evidence") or "[]")
        configured = json.loads(policy.get("specific_evidence_requirements") or "[]")
        if not configured:
            configured = json.loads(policy.get("required_documents") or "[]")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(support, list) or not isinstance(configured, list):
        return False
    if any(not isinstance(item, dict) for item in support):
        return False
    expected = {item.strip() for item in configured if isinstance(item, str) and item.strip()}
    supplied = {item.get("requirement", "").strip() for item in support}
    return bool(expected) and supplied == expected and len(supplied) == len(support) and all(
        len(str(item.get("source_location", "")).strip()) >= 3
        and len(str(item.get("supporting_excerpt", "")).strip()) >= 20
        for item in support
    )


def compare_documents(state: EvidenceState) -> dict:
    case = state["case"]
    references = [
        {
            "policy_id": item["policy_id"],
            "document_type": item["document_type"],
            "effective_date": item["effective_date"],
            "version": item["version"],
            "payer": item.get("payer"),
            "service_type": item.get("service_type"),
            "source_reference": item.get("source_reference"),
            "verification_status": item.get("verification_status") or "MOCK_DEMO",
            "policy_use": (
                "AUTHORITATIVE"
                if item.get("verification_status") == "VERIFIED_PUBLIC"
                else "DEMO_ONLY"
            ),
            "source_url": item.get("source_url"),
            "authoritative_title": item.get("authoritative_title"),
            "authoritative_version": item.get("authoritative_version"),
            "source_verified_by": item.get("source_verified_by"),
            "source_verified_at": item.get("source_verified_at"),
            "required_categories": json.loads(item.get("required_categories") or "[]"),
            "specific_evidence_requirements": json.loads(
                item.get("specific_evidence_requirements") or "[]"
            ),
            "similarity": item.get("similarity", 0.0),
        }
        for item in state["policies"]
    ]
    if not state["policy_valid"]:
        return {
            "result": {
                "case_id": case["id"],
                "policy_mode": state.get("policy_mode", "UNAVAILABLE"),
                "required_documents": [],
                "found_documents": [],
                "possible_evidence_matches": [],
                "missing_documents": [],
                "unreadable_documents": [],
                "possible_mismatches": [state["validation_reason"]],
                "verification_status": "NEEDS_REVIEW",
                "explanation": state["validation_reason"],
                "recommended_action": "Route to a human evidence reviewer to resolve requirement coverage.",
                "source_references": references,
                "needs_human_review": True,
                "evidence_validations": [],
            }
        }

    required: list[str] = []
    policy_categories: dict[str, list[str]] = {}
    for policy in state["policies"]:
        configured = json.loads(policy.get("required_documents") or "[]")
        specific = json.loads(policy.get("specific_evidence_requirements") or "[]")
        categories = json.loads(policy.get("required_categories") or "[]")
        requirements = specific or configured
        for item in requirements:
            name = str(item).strip()
            if name:
                required.append(name)
                policy_categories[name] = list(dict.fromkeys(
                    [*policy_categories.get(name, []), *(str(value).strip() for value in categories if str(value).strip())]
                ))
    required = list(dict.fromkeys(required))

    found: list[dict] = []
    possible_matches: list[dict] = []
    unreadable: list[dict] = []
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
            unreadable.append(
                {
                    "document_name": document_name,
                    "category": document.get("category") or "",
                    "ocr_status": document.get("ocr_status") or "",
                    "reason": reason,
                }
            )
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
                    "category": document.get("category") or "",
                    "ocr_status": document.get("ocr_status") or "",
                    "reason": "OCR text is too short to reliably assess evidence.",
                }
            )
            continue
        processed_documents.append(document)

        for required_document in required:
            for page_number, page in enumerate(normalized_pages, start=1):
                match_status, excerpt = _evidence_match(page, required_document)
                if not match_status:
                    continue
                match = {
                    "required_document": required_document,
                    "document_name": document_name,
                    "category": document.get("category") or "",
                    "ocr_status": document.get("ocr_status") or "",
                    "page_number": page_number,
                    "excerpt": excerpt,
                }
                (found if match_status == "FOUND_IN_TEXT" else possible_matches).append(match)

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

    # Deterministic Evidence Validation per requirement:
    # Distinguishes Document Classification (what category was uploaded)
    # from Evidence Validation (whether medical necessity requirement is satisfied).
    # A document's category alone must NEVER establish that a medical-necessity requirement is satisfied.
    # Supported statuses: SATISFIED, MISSING, UNCERTAIN, CONFLICTING.
    verified_docs = [
        doc for doc in state["documents"]
        if doc.get("status") == "VERIFIED"
    ]
    verified_names = {
        (doc.get("original_filename") or doc.get("name") or "").strip().lower()
        for doc in verified_docs
    }

    category_synonyms = {
        "clinical notes": {"clinical notes", "clinical reports"},
        "conservative treatment history": {"treatment history", "treatment reports"},
        "lab results": {"laboratory reports", "lab-report", "lab report"},
        "laboratory reports": {"laboratory reports", "lab-report", "lab report"},
        "infusion order": {"other supporting evidence", "prescription", "prescription or medication records"},
        "specialist letter": {"medical necessity documentation", "medical necessity documents", "clinical notes"},
        "prescription": {"prescription", "prescription or medication records"},
    }

    has_conflicts = bool(state.get("validation_reason") and "conflict" in state["validation_reason"].lower())

    evidence_validations: list[dict] = []
    for req in required:
        req_norm = req.strip().lower()
        exact_matches = [m for m in found if m["required_document"] == req]
        possible_req_matches = [m for m in possible_matches if m["required_document"] == req]
        text_matched = bool(exact_matches or possible_req_matches)

        expected_cats = category_synonyms.get(req_norm, {req_norm})
        category_matched_docs = [
            doc for doc in state["documents"]
            if (doc.get("category") or "").strip().lower() in expected_cats
            or req_norm in (doc.get("category") or "").strip().lower()
            or req_norm in (doc.get("original_filename") or doc.get("name") or "").strip().lower()
        ]
        category_matched = bool(category_matched_docs)

        # A document category alone NEVER satisfies clinical necessity.
        # It is only SATISFIED if a human reviewer has marked the matching document as VERIFIED.
        is_clinically_verified = any(
            doc.get("status") == "VERIFIED"
            for doc in category_matched_docs
        ) or any(
            m.get("document_name", "").strip().lower() in verified_names
            for m in exact_matches
        )

        if has_conflicts:
            val_status = "CONFLICTING"
            val_reason = "Conflicting policy requirements or contradictory records prevent deterministic verification."
        elif is_clinically_verified and (text_matched or category_matched):
            val_status = "SATISFIED"
            val_reason = "Requirement was clinically verified by a qualified human reviewer."
        elif req in missing:
            val_status = "MISSING"
            val_reason = "No document or extracted text matching this requirement was found."
        elif unreadable and not (text_matched or category_matched):
            val_status = "UNCERTAIN"
            val_reason = (
                "Unreadable or unprocessed documents prevent conclusive verification; "
                "OCR failure is not evidence of absence."
            )
        elif category_matched or text_matched:
            val_status = "UNCERTAIN"
            if category_matched and not text_matched:
                val_reason = (
                    "Document category matches requirement, but category alone does not establish "
                    "medical necessity; clinical verification by a qualified reviewer is required."
                )
            else:
                val_reason = (
                    "Requirement text matched in OCR output, but text match alone does not establish "
                    "clinical validity; clinical verification by a qualified reviewer is required."
                )
        else:
            val_status = "UNCERTAIN"
            val_reason = "Insufficient evidence to determine requirement status; human clinical review required."

        evidence_validations.append(
            {
                "requirement": req,
                "policy_required_categories": policy_categories.get(req, []),
                "validation_status": val_status,
                "category_matched": category_matched,
                "text_matched": text_matched,
                "clinically_verified": is_clinically_verified,
                "reason": val_reason,
                "human_review_required": val_status != "SATISFIED",
            }
        )

    if unreadable:
        action = "Resolve unreadable or unprocessed documents, then rerun the Evidence Agent; OCR failure is not evidence of absence."
    elif missing:
        action = f"Request {missing[0]} from the care team for human review."
    else:
        action = "Ask a qualified human reviewer to verify each text match and requirement; no authorization decision is made."

    return {
        "result": {
            "case_id": case["id"],
            "policy_mode": state.get("policy_mode", "DEMO_ONLY"),
            "required_documents": required,
            "found_documents": found,
            "possible_evidence_matches": possible_matches,
            "missing_documents": missing,
            "unreadable_documents": unreadable,
            "possible_mismatches": [],
            "verification_status": "NEEDS_REVIEW",
            "explanation": "",
            "recommended_action": action,
            "source_references": references,
            "needs_human_review": True,
            "evidence_validations": evidence_validations,
        }
    }


def explain_finding(state: EvidenceState) -> dict:
    if not state.get("policy_valid", True) and state["result"].get("explanation"):
        # Validation failures already carry a deterministic, source-grounded
        # explanation. Do not replace it with generated wording.
        return {
            "result": state["result"],
            "explanation_engine": "deterministic_policy_validation",
        }
    if state.get("policy_mode") == "DEMO_ONLY":
        return {
            "result": {
                **state["result"],
                "explanation": (
                    "Demo policy requirements were used for this demonstration only. "
                    "They are not authoritative payer guidance and require policy review before operational use."
                ),
                "needs_human_review": True,
            },
            "explanation_engine": "deterministic_demo_policy_notice",
        }
    explanation, engine = explain_evidence_with_ollama(state["result"])
    return {"result": {**state["result"], "explanation": explanation}, "explanation_engine": engine}


def audit_finding(state: EvidenceState) -> dict:
    result = state["result"]
    now = state["now"]
    with connect() as db:
        db.execute(
            "INSERT INTO evidence_recommendations(case_id,created_at,verification_status,required_documents,missing_documents,possible_mismatches,explanation,recommended_action,source_references,needs_human_review) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                result["case_id"],
                now,
                result["verification_status"],
                json.dumps(result["required_documents"]),
                json.dumps(result["missing_documents"]),
                json.dumps(result["possible_mismatches"]),
                result["explanation"],
                result["recommended_action"],
                json.dumps(result["source_references"]),
                1,
            ),
        )
        detail = json.dumps(
            {
                "verification_status": result["verification_status"],
                "found_documents": result["found_documents"],
                "possible_evidence_matches": result["possible_evidence_matches"],
                "missing_documents": result["missing_documents"],
                "unreadable_documents": result["unreadable_documents"],
                "possible_mismatches": result["possible_mismatches"],
                "recommended_action": result["recommended_action"],
                "source_references": result["source_references"],
                "evidence_validations": result.get("evidence_validations", []),
                "retrieval_engine": state["retrieval_engine"],
                "explanation_engine": state.get("explanation_engine", "not_run"),
            },
            sort_keys=True,
        )
        db.execute(
            "INSERT INTO audit_events(case_id,timestamp,actor,action,detail,decision_type) VALUES(?,?,?,?,?,?)",
            (result["case_id"], now, "Evidence RAG Agent", "EVIDENCE_RAG_FINDING", detail, "AGENT_RECOMMENDATION_REQUIRES_HUMAN_REVIEW"),
        )
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
