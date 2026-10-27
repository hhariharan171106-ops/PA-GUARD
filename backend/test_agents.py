import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import database
from database import initialize
from agents import run_sla_rag
import agents
import rag


class SlaRagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_document_storage_dir = database.DOCUMENT_STORAGE_DIR
        cls._temp_dir = tempfile.TemporaryDirectory(prefix="pa-guard-sla-tests-")
        database.DOCUMENT_STORAGE_DIR = Path(cls._temp_dir.name) / "documents"
        cls._connection = sqlite3.connect(":memory:", check_same_thread=False)
        cls._connection.row_factory = sqlite3.Row
        cls._patches = ExitStack()
        for target in ("database.connect", "agents.connect", "rag.connect"):
            cls._patches.enter_context(patch(target, return_value=cls._connection))
        cls._patches.enter_context(
            patch("rag.embed_text", return_value=([1.0] * 256, "test-hash"))
        )
        cls._patches.enter_context(
            patch("agents.explain_with_ollama", return_value=("Test rationale.", "test"))
        )
        initialize()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._patches.close()
        cls._connection.close()
        database.DOCUMENT_STORAGE_DIR = cls._original_document_storage_dir
        cls._temp_dir.cleanup()

    def test_breached_case_is_deterministic(self) -> None:
        result = run_sla_rag("PA-1041", datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["risk_level"], "BREACHED")
        self.assertEqual(result["remaining_hours"], -1.0)
        self.assertTrue(result["needs_human_review"])
        self.assertGreater(len(result["source_references"]), 0)

    def test_agent_never_returns_authorization_decision(self) -> None:
        result = run_sla_rag("PA-1042", datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc))
        self.assertNotIn(result["recommended_action"].lower(), {"approve", "deny"})
        self.assertIn(result["risk_level"], {"LOW", "MEDIUM", "HIGH", "BREACHED", "NEEDS_REVIEW"})


if __name__ == "__main__":
    unittest.main()
