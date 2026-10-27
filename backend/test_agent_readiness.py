import json
import sqlite3
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import main
import rag
from agents import explain as explain_sla
from escalation_agents import explain_decision
from evidence_agents import explain_finding
from recovery_agents import execute_recovery, request_approval
from fastapi.testclient import TestClient


class AgentReadinessTests(unittest.TestCase):
    def test_agent_status_reports_qwen_and_graph_availability(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        with (
            patch("main.connect", return_value=connection),
            patch(
                "main.ollama_status",
                return_value={
                    "status": "ready",
                    "model": "qwen3.5:latest",
                    "model_available": True,
                    "message": "Configured Ollama model is available.",
                },
            ),
        ):
            result = main.agent_status()

        self.assertEqual(result["api_status"], "ready")
        self.assertEqual(result["database_status"], "ready")
        self.assertEqual(
            result["agents"],
            {
                "sla": "ready",
                "evidence": "ready",
                "escalation": "ready",
                "recovery": "ready",
            },
        )
        self.assertEqual(result["ollama"]["model"], "qwen3.5:latest")
        self.assertTrue(result["ollama"]["model_available"])

    def test_fastapi_registers_each_langgraph_agent_route(self) -> None:
        routes = {
            (route.path, method)
            for route in main.app.routes
            for method in getattr(route, "methods", set())
        }

        self.assertIn(("/api/agents/sla/cases/{case_id}/recommend", "POST"), routes)
        self.assertIn(("/api/agents/evidence/cases/{case_id}/review", "POST"), routes)
        self.assertIn(("/api/agents/escalation/cases/{case_id}/evaluate", "POST"), routes)
        self.assertIn(("/api/agents/recovery/cases/{case_id}/recover", "POST"), routes)
        self.assertIn(("/api/agents/status", "GET"), routes)

    def test_each_langgraph_agent_runs_through_fastapi_in_isolated_sqlite_state(self) -> None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(connection.close)
        embeddings = ([1.0] * 256, "test-hash")
        with (
            patch("database.connect", return_value=connection),
            patch("main.connect", return_value=connection),
            patch("approval_api.connect", return_value=connection),
            patch("security.connect", return_value=connection),
            patch("agents.connect", return_value=connection),
            patch("evidence_agents.connect", return_value=connection),
            patch("escalation_agents.connect", return_value=connection),
            patch("recovery_agents.connect", return_value=connection),
            patch("main.index_policies", return_value={"indexed": 0}),
            patch("rag.embed_text", return_value=embeddings),
            patch("evidence_agents.embed_text", return_value=embeddings),
            patch("escalation_agents.embed_text", return_value=embeddings),
            patch("recovery_agents.embed_text", return_value=embeddings),
            patch(
                "agents.explain_with_ollama",
                return_value=("I reviewed the deadline and evidence dependencies.", "ollama:qwen3.5:latest"),
            ),
            patch(
                "evidence_agents.explain_evidence_with_ollama",
                return_value=("I compared the uploaded documents with the approved checklist.", "ollama:qwen3.5:latest"),
            ),
            patch(
                "escalation_agents.explain_escalation_with_ollama",
                return_value=("I checked the current escalation thresholds for this case.", "ollama:qwen3.5:latest"),
            ),
            patch(
                "recovery_agents.explain_recovery_with_ollama",
                return_value=("I reviewed the notification issue; no recovery action was taken.", "ollama:qwen3.5:latest"),
            ),
            TestClient(main.app) as client,
        ):
            login = client.post("/api/auth/login", json={"username":"manager","password":"ManagerDemo1"})
            self.assertEqual(login.status_code, 200, login.text)
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            sla = client.post(
                "/api/agents/sla/cases/PA-1042/recommend",
                headers=headers,
                json={"actor": "Test reviewer"},
            )
            evidence = client.post(
                "/api/agents/evidence/cases/PA-1042/review",
                headers=headers,
                json={"actor": "Test reviewer"},
            )
            escalation = client.post(
                "/api/agents/escalation/cases/PA-1042/evaluate",
                headers=headers,
                json={"actor": "Test reviewer", "approve_notification": False},
            )
            recovery = client.post(
                "/api/agents/recovery/cases/PA-1042/recover",
                headers=headers,
                json={
                    "actor": "Test reviewer",
                    "error_code": "NOTIFY_TIMEOUT",
                    "approve_recovery": False,
                    "preview_only": True,
                },
            )

        self.assertEqual(sla.status_code, 200, sla.text)
        self.assertEqual(sla.json()["explanation_engine"], "ollama:qwen3.5:latest")
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertEqual(
            evidence.json()["finding"]["explanation_engine"],
            "ollama:qwen3.5:latest",
        )
        self.assertEqual(escalation.status_code, 200, escalation.text)
        self.assertEqual(
            escalation.json()["decision"]["explanation_engine"],
            "ollama:qwen3.5:latest",
        )
        self.assertEqual(recovery.status_code, 200, recovery.text)
        self.assertEqual(
            recovery.json()["recovery"]["recovery_status"],
            "RECOMMENDATION_ONLY",
        )
        self.assertEqual(
            recovery.json()["recovery"]["explanation_engine"],
            "ollama:qwen3.5:latest",
        )

    def test_qwen_status_detects_installed_model(self) -> None:
        response = BytesIO(
            json.dumps({"models": [{"name": "qwen3.5:latest"}]}).encode()
        )
        with (
            patch.object(rag, "OLLAMA_MODEL", "qwen3.5:latest"),
            patch("rag.urlopen", return_value=response),
        ):
            status = rag.ollama_status()

        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["model_available"])

    def test_qwen_status_surfaces_unavailable_service(self) -> None:
        with patch("rag.urlopen", side_effect=URLError(ConnectionRefusedError("connection refused"))):
            status = rag.ollama_status()

        self.assertEqual(status["status"], "unavailable")
        self.assertFalse(status["model_available"])
        self.assertIn("Start Ollama", status["message"])

    def test_qwen_status_identifies_reachable_service_with_missing_model(self) -> None:
        response = BytesIO(json.dumps({"models": [{"name": "another-model:latest"}]}).encode())
        with (
            patch.object(rag, "OLLAMA_MODEL", "qwen3.5:latest"),
            patch("rag.urlopen", return_value=response),
        ):
            status = rag.ollama_status()

        self.assertEqual(status["status"], "model_missing")
        self.assertFalse(status["model_available"])
        self.assertIn("ollama pull qwen3.5:latest", status["message"])

    def test_generation_fallback_retains_safe_ollama_failure_diagnostic(self) -> None:
        facts = {
            "risk_level": "MEDIUM",
            "remaining_time": "24 hours",
            "rule_reason": "Synthetic test facts only.",
            "recommended_action": "Continue human review.",
        }
        with patch("rag.urlopen", side_effect=URLError(ConnectionRefusedError("connection refused"))):
            explanation, engine = rag.explain_with_ollama(facts)

        self.assertIn("deadline is 24 hours away", explanation)
        self.assertEqual(engine, "deterministic-template-fallback")
        self.assertIn("Connection to Ollama was refused", rag.last_ollama_error())

    def test_generation_404_explains_missing_model_and_keeps_fallback(self) -> None:
        error = HTTPError(
            "http://localhost:11434/api/generate",
            404,
            "model not found",
            None,
            BytesIO(b'{"error":"model not found"}'),
        )
        facts = {
            "risk_level": "LOW",
            "remaining_time": "48 hours",
            "rule_reason": "Synthetic test facts only.",
            "recommended_action": "Continue human review.",
        }
        with patch("rag.urlopen", side_effect=error):
            explanation, engine = rag.explain_with_ollama(facts)

        self.assertIn("I checked the case against the current SLA rules", explanation)
        self.assertEqual(engine, "deterministic-template-fallback")
        self.assertIn("HTTP 404", rag.last_ollama_error())
        self.assertIn("ollama pull qwen3.5:latest", rag.last_ollama_error())

    def test_workflow_llm_prompt_is_bounded_and_omits_filenames_and_ocr_excerpts(self) -> None:
        facts = {
            "case_status": "NEW",
            "case_risk": "ON TRACK",
            "deadline": "2099-10-23T18:30:00+00:00",
            "documents": [{
                "filename": "PRIVATE-FILENAME.pdf",
                "category": "Clinical Reports",
                "ocr_status": "COMPLETED",
                "extracted_text_available": True,
                "extracted_text": "PRIVATE OCR CONTENT",
            }],
            "evidence_items": [{
                "requirement": "Clinical notes",
                "status": "NEEDS_REVIEW",
                "human_review_required": True,
                "supporting_documents": [{"excerpt": "PRIVATE EXCERPT"}],
            }],
            "sla": {"risk_level": "LOW", "recommended_action": "Continue review."},
            "sla_risk_prediction": {"available": False},
            "escalation": {"escalation_level": "NONE", "reason": "No rule triggered."},
            "recovery": {"recovery_status": "RECOMMENDATION_ONLY"},
            "failed_stages": {},
            "recommended_next_action": "Continue human review.",
        }
        with patch.object(
            rag,
            "_ollama",
            return_value={"response": "The case is on track and needs human evidence review."},
        ) as generate:
            rag.explain_case_workflow_with_ollama(facts)

        request_payload = generate.call_args.args[1]
        self.assertEqual(request_payload["options"]["num_predict"], 96)
        self.assertEqual(request_payload["options"]["num_ctx"], 1536)
        self.assertNotIn("PRIVATE-FILENAME.pdf", request_payload["prompt"])
        self.assertNotIn("PRIVATE OCR CONTENT", request_payload["prompt"])
        self.assertNotIn("PRIVATE EXCERPT", request_payload["prompt"])

    def test_recovery_preview_cannot_enter_execution(self) -> None:
        plan = {
            "likely_cause": "The notification service did not confirm delivery.",
            "recommended_recovery": "Retry the simulated notification once.",
            "retry_allowed": True,
            "human_approval_required": False,
            "source_references": [],
        }
        preview = request_approval({"preview_only": True, "plan": plan})["result"]

        self.assertEqual(preview["recovery_status"], "RECOMMENDATION_ONLY")
        self.assertTrue(preview["human_approval_required"])
        self.assertEqual(execute_recovery({"result": preview}), {})

    def test_generated_reasoning_is_removed_from_chat_text(self) -> None:
        response = "<think>internal reasoning</think>I reviewed the SLA facts."

        self.assertEqual(
            rag._clean_generated_response(response),
            "I reviewed the SLA facts.",
        )

    def test_agents_generate_rationales_when_policy_validation_fails(self) -> None:
        with (
            patch(
                "agents.explain_with_ollama",
                return_value=("Policy coverage is incomplete, so a human must review this case.", "ollama:test"),
            ) as sla_explanation,
            patch(
                "evidence_agents.explain_evidence_with_ollama",
                return_value=("No current requirements were available, so evidence cannot be assessed.", "ollama:test"),
            ) as evidence_explanation,
            patch(
                "escalation_agents.explain_escalation_with_ollama",
                return_value=("Escalation is paused until policy coverage is resolved.", "ollama:test"),
            ) as escalation_explanation,
        ):
            sla = explain_sla(
                {
                    "policy_valid": False,
                    "result": {
                        "risk_level": "NEEDS_REVIEW",
                        "remaining_time": "12 hours",
                        "rule_reason": "No approved SLA policy was retrieved.",
                        "recommended_action": "Route this case to an SLA manager.",
                    },
                }
            )
            evidence = explain_finding(
                {
                    "policy_valid": False,
                    "result": {
                        "verification_status": "NEEDS_REVIEW",
                        "required_documents": [],
                        "found_documents": [],
                        "possible_evidence_matches": [],
                        "missing_documents": [],
                        "unreadable_documents": [],
                        "possible_mismatches": ["No approved payer requirements were retrieved."],
                        "recommended_action": "Route to a human evidence reviewer.",
                    },
                }
            )
            escalation = explain_decision(
                {
                    "policy_valid": False,
                    "result": {
                        "escalation_required": False,
                        "escalation_level": "NEEDS_REVIEW",
                        "reason": "No current approved escalation policy applies.",
                        "responsible_role": "Human policy reviewer",
                        "recommended_action": "Resolve policy coverage before notification.",
                        "approval_required": True,
                        "notification_status": "NOT_SENT",
                    },
                }
            )

        self.assertIn("Policy coverage is incomplete", sla["result"]["explanation"])
        self.assertEqual(sla["explanation_engine"], "ollama:test")
        self.assertIn("No current requirements", evidence["result"]["explanation"])
        self.assertEqual(evidence["explanation_engine"], "ollama:test")
        self.assertIn("paused until policy coverage", escalation["result"]["reason"])
        self.assertEqual(escalation["explanation_engine"], "ollama:test")
        sla_explanation.assert_called_once()
        evidence_explanation.assert_called_once()
        escalation_explanation.assert_called_once()

    def test_qwen_generation_disables_hidden_thinking_and_caps_reply(self) -> None:
        facts = {
            "risk_level": "MEDIUM",
            "remaining_time": "12 hours",
            "rule_reason": "The deadline has 12 hours remaining.",
            "recommended_action": "Continue assigned workflow.",
            "case_id": "PA-1",
        }
        with patch.object(
            rag,
            "_ollama",
            return_value={"response": "I reviewed the deadline and found medium risk."},
        ) as generate:
            explanation, engine = rag.explain_with_ollama(facts)

        request_payload = generate.call_args.args[1]
        self.assertEqual(request_payload["model"], "qwen3.5:latest")
        self.assertFalse(request_payload["think"])
        self.assertEqual(request_payload["options"]["num_predict"], 128)
        self.assertEqual(request_payload["options"]["num_ctx"], 2048)
        self.assertNotIn("case_id", request_payload["prompt"])
        self.assertIn("user-facing rationale", request_payload["prompt"])
        self.assertIn("private chain-of-thought", request_payload["prompt"])
        self.assertIn("I reviewed", explanation)
        self.assertEqual(engine, "ollama:qwen3.5:latest")

    def test_breached_sla_uses_llm_for_human_readable_rationale(self) -> None:
        facts = {
            "risk_level": "BREACHED",
            "remaining_hours": -12.5,
            "rule_reason": (
                "The deadline has -12.5 hours remaining and the case has "
                "2 unresolved evidence dependencies."
            ),
        }

        with patch.object(
            rag,
            "_ollama",
            return_value={"response": "The deadline passed 12.5 hours ago and two evidence items remain unresolved."},
        ) as generate:
            explanation, engine = rag.explain_with_ollama(facts)

        generate.assert_called_once()
        self.assertIn("passed 12.5 hours ago", explanation)
        self.assertIn("private chain-of-thought", generate.call_args.args[1]["prompt"])
        self.assertEqual(engine, "ollama:qwen3.5:latest")

    def test_evidence_explanation_does_not_send_extracted_text_to_qwen(self) -> None:
        facts = {
            "verification_status": "NEEDS_REVIEW",
            "required_documents": ["Infusion order"],
            "found_documents": [
                {"required_document": "Infusion order", "excerpt": "sensitive OCR text"}
            ],
            "possible_evidence_matches": [],
            "missing_documents": [],
            "unreadable_documents": [],
            "possible_mismatches": [],
            "recommended_action": "Ask a reviewer to verify the match.",
            "case_id": "PA-1",
        }
        with patch.object(
            rag,
            "_ollama",
            return_value={"response": "A reviewer should verify the infusion order."},
        ) as generate:
            rag.explain_evidence_with_ollama(facts)

        prompt = generate.call_args.args[1]["prompt"]
        self.assertNotIn("sensitive OCR text", prompt)
        self.assertNotIn("case_id", prompt)

    def test_escalation_explanation_uses_only_bounded_operational_facts(self) -> None:
        facts = {
            "case_id": "PA-1",
            "escalation_level": "LEVEL_1",
            "responsible_role": "SLA Manager",
            "reason": "Threshold reached.",
            "recommended_action": "Request human review.",
            "approval_required": True,
            "notification_status": "PENDING_APPROVAL",
            "source_references": [{"policy_id": "POLICY"}],
        }
        with patch.object(
            rag,
            "_ollama",
            return_value={"response": "Please review this escalation."},
        ) as generate:
            rag.explain_escalation_with_ollama(facts)

        prompt = generate.call_args.args[1]["prompt"]
        self.assertNotIn("case_id", prompt)
        self.assertNotIn("source_references", prompt)

    def test_duplicate_escalation_uses_llm_for_bounded_rationale(self) -> None:
        facts = {
            "escalation_level": "LEVEL_2",
            "notification_status": "DUPLICATE_SUPPRESSED",
            "remaining_hours": -12,
            "reason": "A prior notification for this level already exists.",
            "recommended_action": "Review the existing notification record.",
            "approval_required": True,
        }

        with patch.object(
            rag,
            "_ollama",
            return_value={"response": "A prior notification exists, so review that record instead of sending another."},
        ) as generate:
            explanation, engine = rag.explain_escalation_with_ollama(facts)

        generate.assert_called_once()
        self.assertIn("DUPLICATE_SUPPRESSED", generate.call_args.args[1]["prompt"])
        self.assertIn("private chain-of-thought", generate.call_args.args[1]["prompt"])
        self.assertIn("prior notification exists", explanation)
        self.assertEqual(engine, "ollama:qwen3.5:latest")


if __name__ == "__main__":
    unittest.main()
