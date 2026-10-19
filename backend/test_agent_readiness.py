import json
import sqlite3
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError

import main
import rag
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
            sla = client.post(
                "/api/agents/sla/cases/PA-1042/recommend",
                json={"actor": "Test reviewer"},
            )
            evidence = client.post(
                "/api/agents/evidence/cases/PA-1042/review",
                json={"actor": "Test reviewer"},
            )
            escalation = client.post(
                "/api/agents/escalation/cases/PA-1042/evaluate",
                json={"actor": "Test reviewer", "approve_notification": False},
            )
            recovery = client.post(
                "/api/agents/recovery/cases/PA-1042/recover",
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
        with patch("rag.urlopen", side_effect=URLError("offline")):
            status = rag.ollama_status()

        self.assertEqual(status["status"], "unavailable")
        self.assertFalse(status["model_available"])

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
        self.assertEqual(request_payload["options"]["num_predict"], 64)
        self.assertEqual(request_payload["options"]["num_ctx"], 2048)
        self.assertNotIn("case_id", request_payload["prompt"])
        self.assertIn("I reviewed", explanation)
        self.assertEqual(engine, "ollama:qwen3.5:latest")

    def test_breached_sla_uses_fast_human_readable_fallback(self) -> None:
        facts = {
            "risk_level": "BREACHED",
            "remaining_hours": -12.5,
            "rule_reason": (
                "The deadline has -12.5 hours remaining and the case has "
                "2 unresolved evidence dependencies."
            ),
        }

        with patch.object(rag, "_ollama") as generate:
            explanation, engine = rag.explain_with_ollama(facts)

        generate.assert_not_called()
        self.assertIn("passed 12.5 hours ago", explanation)
        self.assertIn("2 unresolved evidence dependencies", explanation)
        self.assertEqual(engine, "deterministic-template-fallback")

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

    def test_duplicate_escalation_uses_deterministic_safe_wording(self) -> None:
        facts = {
            "escalation_level": "LEVEL_2",
            "notification_status": "DUPLICATE_SUPPRESSED",
            "remaining_hours": -12,
        }

        with patch.object(rag, "_ollama") as generate:
            explanation, engine = rag.explain_escalation_with_ollama(facts)

        generate.assert_not_called()
        self.assertIn("duplicate was suppressed", explanation)
        self.assertIn("review the existing record", explanation)
        self.assertNotIn("received", explanation)
        self.assertEqual(engine, "deterministic-template-fallback")


if __name__ == "__main__":
    unittest.main()
