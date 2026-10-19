import hashlib
import json
import math
import os
import re
from datetime import date
from functools import lru_cache
from urllib.error import URLError
from urllib.request import Request, urlopen

from database import connect

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:latest")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_GENERATION_TIMEOUT_SECONDS = int(
    os.getenv("OLLAMA_GENERATION_TIMEOUT_SECONDS", "45")
)
OLLAMA_GENERATION_OPTIONS = {"temperature": 0, "num_predict": 64, "num_ctx": 2048}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _local_embedding(text: str, dimensions: int = 256) -> list[float]:
    vector = [0.0] * dimensions
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / magnitude for value in vector]


def _ollama(path: str, payload: dict, timeout: int = 20) -> dict:
    request = Request(
        f"{OLLAMA_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ollama_status() -> dict:
    request = Request(f"{OLLAMA_URL}/api/tags", method="GET")
    try:
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, ValueError):
        return {
            "status": "unavailable",
            "model": OLLAMA_MODEL,
            "model_available": False,
            "message": "Ollama could not be reached.",
        }

    models = result.get("models", [])
    if not isinstance(models, list):
        raise ValueError("Ollama returned an invalid model list")
    installed_models = {
        model.get("name")
        for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    }
    model_available = OLLAMA_MODEL in installed_models
    return {
        "status": "ready" if model_available else "model_missing",
        "model": OLLAMA_MODEL,
        "model_available": model_available,
        "message": (
            "Configured Ollama model is available."
            if model_available
            else f"Configured model {OLLAMA_MODEL} is not installed in Ollama."
        ),
    }


@lru_cache(maxsize=1)
def _installed_ollama_models() -> frozenset[str]:
    request = Request(f"{OLLAMA_URL}/api/tags", method="GET")
    try:
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, ValueError):
        return frozenset()

    models = result.get("models", [])
    if not isinstance(models, list):
        return frozenset()
    return frozenset(
        model["name"]
        for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    )


def _clean_generated_response(response: str) -> str:
    without_thinking = re.sub(
        r"<think>.*?</think>", "", response, flags=re.IGNORECASE | re.DOTALL
    )
    return without_thinking.strip()


def embed_text(text: str) -> tuple[list[float], str]:
    if OLLAMA_EMBED_MODEL not in _installed_ollama_models():
        return _local_embedding(text), "local-hash-fallback"
    try:
        result = _ollama(
            "/api/embed",
            {"model": OLLAMA_EMBED_MODEL, "input": text},
            timeout=5,
        )
        embeddings = result.get("embeddings", [])
        if embeddings:
            return embeddings[0], f"ollama:{OLLAMA_EMBED_MODEL}"
    except (URLError, TimeoutError, OSError, ValueError, KeyError):
        pass
    return _local_embedding(text), "local-hash-fallback"


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0


def index_policies() -> dict:
    indexed = 0
    engine = ""
    with connect() as db:
        rows = db.execute("SELECT id, content FROM policy_documents WHERE approved=1").fetchall()
        for row in rows:
            embedding, engine = embed_text(row["content"])
            db.execute("UPDATE policy_documents SET embedding=?, embedding_engine=? WHERE id=?", (json.dumps(embedding), engine, row["id"]))
            indexed += 1
    return {"indexed": indexed, "embedding_engine": engine or "none"}


def retrieve_policies(query: str, limit: int = 6) -> tuple[list[dict], str]:
    query_embedding, engine = embed_text(query)
    with connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM policy_documents WHERE approved=1")]
    scored: list[dict] = []
    required_types = {"SLA_POLICY", "WORKFLOW_GUIDELINE", "ESCALATION_RULE"}
    query_lower = query.lower()
    for row in rows:
        stored = json.loads(row["embedding"]) if row.get("embedding") else None
        if stored is None or row.get("embedding_engine") != engine:
            stored, _ = embed_text(row["content"])
        similarity = round(cosine_similarity(query_embedding, stored), 4)
        row["similarity"] = similarity
        row["priority_boost"] = 1.0 if row.get("document_type") in required_types else 0.0
        if row.get("document_type") in required_types:
            row["similarity"] += 0.5
        if any(keyword in query_lower for keyword in ("sla", "deadline", "escalation", "workflow", "evidence")) and row.get("document_type") in required_types:
            row["similarity"] += 0.25
        row.pop("embedding", None)
        scored.append(row)

    scored.sort(key=lambda item: (item["similarity"], item["document_type"] in required_types), reverse=True)
    selected: list[dict] = []
    seen_types: set[str] = set()
    for item in scored:
        doc_type = item.get("document_type")
        if doc_type in required_types and doc_type not in seen_types:
            selected.append(item)
            seen_types.add(doc_type)
    for item in scored:
        if item not in selected and len(selected) < limit:
            selected.append(item)
    if len(selected) < limit:
        selected.extend(item for item in scored if item not in selected)[: limit - len(selected)]
    return selected[:limit], engine


def validate_policies(policies: list[dict], as_of: date) -> tuple[bool, str]:
    if not policies:
        return False, "No approved SLA policy was retrieved."
    valid = [item for item in policies if date.fromisoformat(item["effective_date"]) <= as_of and (not item["expires_at"] or date.fromisoformat(item["expires_at"]) >= as_of)]
    if not valid:
        return False, "Retrieved policies are not effective on the calculation date."
    by_type: dict[str, set[str]] = {}
    for item in valid:
        by_type.setdefault(item["document_type"], set()).add(item["version"])
    conflicts = [kind for kind, versions in by_type.items() if len(versions) > 1]
    if conflicts:
        return False, f"Conflicting effective policy versions: {', '.join(conflicts)}."
    required = {"SLA_POLICY", "WORKFLOW_GUIDELINE", "ESCALATION_RULE"}
    missing = required - set(by_type)
    if missing:
        return False, f"Missing required policy types: {', '.join(sorted(missing))}."
    return True, "Approved policy set is current and internally consistent."


def explain_with_ollama(facts: dict) -> tuple[str, str]:
    if facts.get("risk_level") == "BREACHED":
        hours = abs(facts.get("remaining_hours", 0))
        dependency_text = facts["rule_reason"].split(" and the case has ", 1)[-1]
        explanation = (
            f"The SLA deadline passed {hours:g} hours ago, so this case is marked breached. "
            f"It has {dependency_text}"
        )
        return explanation, "deterministic-template-fallback"

    explanation_facts = {
        key: facts[key]
        for key in (
            "risk_level",
            "remaining_time",
            "rule_reason",
            "recommended_action",
        )
        if key in facts
    }
    prompt = (
        "In one short, friendly sentence, restate these SLA facts. Add no facts or inferences "
        "and do not make clinical or authorization decisions.\n"
        f"FACTS: {json.dumps(explanation_facts, sort_keys=True)}"
    )
    try:
        result = _ollama(
            "/api/generate",
            {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False, "options": OLLAMA_GENERATION_OPTIONS},
            timeout=OLLAMA_GENERATION_TIMEOUT_SECONDS,
        )
        response = _clean_generated_response(str(result.get("response", "")))
        if response:
            return response, f"ollama:{OLLAMA_MODEL}"
    except (URLError, TimeoutError, OSError, ValueError, KeyError):
        pass
    explanation = f"I checked the case against the current SLA rules. The deadline is {facts['remaining_time']} away, and the rules place it in the {facts['risk_level']} category. {facts['rule_reason']}"
    return explanation, "deterministic-template-fallback"


def explain_evidence_with_ollama(facts: dict) -> tuple[str, str]:
    explanation_facts = {
        "verification_status": facts["verification_status"],
        "required_document_count": len(facts["required_documents"]),
        "found_document_count": len(facts["found_documents"]),
        "missing_documents": facts["missing_documents"][:3],
        "unreadable_document_names": [
            item["document_name"] for item in facts["unreadable_documents"][:3]
        ],
        "possible_mismatch_count": len(facts["possible_mismatches"]),
        "recommended_action": facts["recommended_action"],
    }
    prompt = (
        "In one short, friendly sentence, restate these evidence-review facts. "
        "Do not infer document contents or clinical validity, or make an authorization decision.\n"
        f"FACTS: {json.dumps(explanation_facts, sort_keys=True)}"
    )
    try:
        result = _ollama(
            "/api/generate",
            {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False, "options": OLLAMA_GENERATION_OPTIONS},
            timeout=OLLAMA_GENERATION_TIMEOUT_SECONDS,
        )
        response = _clean_generated_response(str(result.get("response", "")))
        if response:
            return response, f"ollama:{OLLAMA_MODEL}"
    except (URLError, TimeoutError, OSError, ValueError, KeyError):
        pass
    missing = facts["missing_documents"]
    mismatches = facts["possible_mismatches"]
    summary = f"{len(missing)} required document(s) are missing and {len(mismatches)} item(s) require human verification."
    return f"I compared the documents with the approved requirements. {summary}", "deterministic-template-fallback"

def explain_escalation_with_ollama(facts: dict) -> tuple[str, str]:
    remaining_hours = facts.get("remaining_hours")
    if facts.get("notification_status") == "DUPLICATE_SUPPRESSED":
        return (
            "A prior notification record exists, so this duplicate was suppressed. "
            "The SLA Manager should review the existing record.",
            "deterministic-template-fallback",
        )
    if isinstance(remaining_hours, (int, float)) and remaining_hours <= 0:
        return (
            "The deadline has passed, so the SLA Manager should review this "
            f"{facts['escalation_level'].replace('_', ' ')} escalation. "
            "Human review is still required.",
            "deterministic-template-fallback",
        )

    explanation_facts = {
        key: facts[key]
        for key in (
            "escalation_required",
            "escalation_level",
            "responsible_role",
            "recommended_action",
            "approval_required",
            "notification_status",
        )
        if key in facts
    }
    if isinstance(remaining_hours, (int, float)):
        explanation_facts["deadline_status"] = (
            f"{remaining_hours:g} hours remain before the deadline."
        )
    prompt = (
        "In one short, friendly sentence, restate these escalation facts. Do not add facts or infer thresholds or recipients; "
        "claim a real notification was delivered, or approve or deny authorization.\n"
        f"FACTS: {json.dumps(explanation_facts, sort_keys=True)}"
    )
    try:
        result = _ollama(
            "/api/generate",
            {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False, "options": OLLAMA_GENERATION_OPTIONS},
            timeout=OLLAMA_GENERATION_TIMEOUT_SECONDS,
        )
        response = _clean_generated_response(str(result.get("response", "")))
        if response:
            return response, f"ollama:{OLLAMA_MODEL}"
    except (URLError, TimeoutError, OSError, ValueError, KeyError):
        pass
    return f"I checked the configured escalation rules. {facts['reason']}", "deterministic-template-fallback"


def explain_recovery_with_ollama(facts: dict) -> tuple[str, str]:
    safe_facts = {
        "failure_type": facts["failure_type"],
        "likely_cause": facts["likely_cause"],
        "recommended_recovery": facts["recommended_recovery"],
        "retry_allowed": facts["retry_allowed"],
        "human_approval_required": facts["human_approval_required"],
        "recovery_status": facts["recovery_status"],
    }
    prompt = (
        "Rewrite these recovery facts in one short, natural sentence to a prior-authorization coordinator. "
        "Use only the supplied operational facts. Do not claim an action was taken, invent policy, or make clinical or authorization decisions.\n"
        f"FACTS: {json.dumps(safe_facts, sort_keys=True)}"
    )
    try:
        result = _ollama(
            "/api/generate",
            {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False, "options": OLLAMA_GENERATION_OPTIONS},
            timeout=OLLAMA_GENERATION_TIMEOUT_SECONDS,
        )
        response = _clean_generated_response(str(result.get("response", "")))
        if response:
            return response, f"ollama:{OLLAMA_MODEL}"
    except (URLError, TimeoutError, OSError, ValueError, KeyError):
        pass
    return (
        f"I reviewed the recorded {facts['failure_type'].replace('_', ' ').lower()} issue. "
        f"{facts['recommended_recovery']} No recovery action was taken.",
        "deterministic-template-fallback",
    )



def source_references(policies: list[dict]) -> list[dict]:
    return [{"policy_id": item["policy_id"], "document_type": item["document_type"], "effective_date": item["effective_date"], "version": item["version"], "similarity": item["similarity"]} for item in policies]
