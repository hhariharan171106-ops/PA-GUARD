import asyncio
from urllib.error import URLError

import httpx
import pytest
from unittest.mock import patch

import case_workflow
import rag


BASE_FACTS = {
    "case_status": "IN_REVIEW",
    "unverified_document_count": 5,
    "sla_risk_prediction": {
        "available": True,
        "risk_score": 65.6,
        "risk_level": "HIGH",
        "breach_probability": 0.656,
        "reliable": False,
        "operational_risk": {"risk_category": "MEDIUM", "remaining_hours": 20, "reasons": ["deadline window"]},
    },
    "evidence_items": [{"requirement": "Clinical records", "status": "NEEDS_REVIEW"}],
    "escalation": {"escalation_level": "NONE", "escalation_required": False},
    "recommended_next_action": "Priority human review.",
}


def test_async_ollama_success_and_telemetry(monkeypatch):
    async def run():
        monkeypatch.setattr(rag, "OLLAMA_URL", "http://ollama.test")
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "response": "Review outstanding items.", "load_duration": 2_000_000,
            "prompt_eval_count": 12, "eval_count": 8,
        }))
        async with httpx.AsyncClient(transport=transport) as client:
            return await rag.generate_case_explanation_async(BASE_FACTS, "test-request", client)
    result = asyncio.run(run())
    assert result["available"] is True
    assert result["diagnostics"]["load_duration_ms"] == 2
    assert result["diagnostics"]["eval_count"] == 8


@pytest.mark.parametrize("handler,category", [
    (lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request)), "connection_failure"),
    (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)), "inference_or_model_load_timeout"),
    (lambda request: httpx.Response(200, text="not-json"), "malformed_response"),
    (lambda request: httpx.Response(200, json={"response": "   "}), "empty_response"),
])
def test_async_ollama_errors_keep_deterministic_fallback(monkeypatch, handler, category):
    async def run():
        monkeypatch.setattr(rag, "OLLAMA_URL", "http://ollama.test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await rag.generate_case_explanation_async(BASE_FACTS, "test-request", client)
    result = asyncio.run(run())
    assert result["available"] is False
    assert result["diagnostics"]["failure_category"] == category
    assert result["engine"] == case_workflow.DETERMINISTIC_EXPLANATION_ENGINE


def test_deterministic_explanation_keeps_risk_and_verification_distinct():
    result = case_workflow.build_deterministic_case_explanation(BASE_FACTS)
    assert "high risk (65.6% estimated chance" in result
    assert "Deadline check: medium" in result
    assert "5 document(s) still need human verification" in result
    assert "Clinical records: NEEDS REVIEW" in result
    assert "trained on synthetic data" in result
    assert "has not been validated for real cases" in result
    assert "What I found" not in result  # deterministic text stays a compact plain-language fallback


def test_ollama_json_output_is_not_shown_as_a_user_explanation():
    assert rag._clean_generated_response('{"risk":"HIGH"}') == ""
    assert rag._clean_generated_response('["raw", "json"]') == ""
    assert rag._clean_generated_response("A reviewer should check the missing item.") == "A reviewer should check the missing item."


def test_case_explanation_prompt_uses_case_facts_and_plain_language():
    prompt = rag.build_case_explanation_prompt(BASE_FACTS)
    assert "simple, professional English" in prompt
    assert "IN_REVIEW" in prompt
    assert "synthetic and unvalidated" in prompt
    assert "infer clinical facts" in prompt


def test_explanation_cache_key_changes_when_analysis_facts_change():
    original = case_workflow._explanation_cache_key("case-1", BASE_FACTS)
    changed = {**BASE_FACTS, "unverified_document_count": 4}
    assert original != case_workflow._explanation_cache_key("case-1", changed)


def test_case_workflow_context_suppresses_optional_agent_generation_calls():
    with patch.object(rag, "urlopen") as urlopen:
        with rag.deterministic_agent_explanations(), pytest.raises(URLError):
            rag._ollama("/api/generate", {"model": "test", "prompt": "test"})
    urlopen.assert_not_called()


def test_duplicate_explanation_requests_share_one_background_inference(monkeypatch):
  async def run():
    calls = 0
    case_workflow._EXPLANATION_CACHE.clear()
    case_workflow._EXPLANATION_FAILURES.clear()
    case_workflow._EXPLANATION_TASKS.clear()
    case_workflow._EXPLANATION_REQUEST_IDS.clear()
    monkeypatch.setattr(case_workflow, "_load_workflow_for_explanation", lambda case_id: BASE_FACTS)
    monkeypatch.setattr(case_workflow, "_audit_explanation", lambda *args: None)

    async def generate(facts, request_id):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {"available": True, "explanation": "Enhanced.", "engine": "ollama:test", "diagnostics": {"model": "test"}}

    monkeypatch.setattr(case_workflow, "generate_case_explanation_async", generate)
    first = await case_workflow.request_case_explanation("case-1")
    second = await case_workflow.request_case_explanation("case-1")
    assert first["status"] == second["status"] == "GENERATING"
    assert first["request_id"] == second["request_id"]
    await asyncio.gather(*list(case_workflow._EXPLANATION_TASKS.values()))
    assert calls == 1
    assert (await case_workflow.case_explanation_status("case-1"))["status"] == "READY"
  asyncio.run(run())


def test_ollama_generation_concurrency_is_bounded(monkeypatch):
  async def run():
    monkeypatch.setattr(rag, "OLLAMA_URL", "http://ollama.test")
    active = 0
    maximum = 0

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={"response": "Review."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*[
            rag.generate_case_explanation_async(BASE_FACTS, f"request-{index}", client)
            for index in range(3)
        ])
    assert maximum == 1
  asyncio.run(run())
