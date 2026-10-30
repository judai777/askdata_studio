from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.model_client import ModelClient, ModelRequestBudgetExceeded


class ModelTelemetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ModelClient(Settings(api_key="test-secret", max_retries=0))
        self.client.telemetry_enabled = True

    def test_success_records_only_safe_metadata(self) -> None:
        self.client.current_stage = "preprocess"
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "answer-secret"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "sensitive_extra": "hidden"},
        }).encode()
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(self.client.chat("private prompt", "private question"), "answer-secret")
        record = self.client.telemetry[0]
        self.assertEqual(record["kind"], "chat")
        self.assertEqual(record["stage"], "preprocess")
        self.assertEqual(record["usage"]["total_tokens"], 10)
        serialized = json.dumps(record)
        for secret in ("test-secret", "private prompt", "private question", "answer-secret", "sensitive_extra"):
            self.assertNotIn(secret, serialized)

    def test_missing_usage_and_failure_are_unknown(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": [{"index": 0, "embedding": [1.0, 0.0]}]}'
        with patch("urllib.request.urlopen", return_value=response):
            self.client.embed(["private embedding"])
        self.assertIsNone(self.client.telemetry[0]["usage"])
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("network-private")):
            with self.assertRaises(RuntimeError):
                self.client.rerank("question", ["schema"], 1)
        self.assertEqual(self.client.telemetry[-1]["kind"], "rerank")
        self.assertFalse(self.client.telemetry[-1]["success"])
        self.assertNotIn("network-private", json.dumps(self.client.telemetry))

    def test_request_budget_counts_retries_before_network_call(self) -> None:
        client = ModelClient(Settings(api_key="test-secret", max_retries=2))
        client.telemetry_enabled = True
        client.configure_request_budget(1)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("network-private")) as request:
            with patch("time.sleep"):
                with self.assertRaises(ModelRequestBudgetExceeded):
                    client.chat("private prompt", "private question")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(client.request_count, 1)
        self.assertTrue(client.request_budget_exhausted)
        self.assertEqual(client.telemetry[0]["attempts"], 1)
        self.assertNotIn("network-private", json.dumps(client.telemetry))

    def test_exact_budget_can_complete_without_exhaustion(self) -> None:
        self.client.configure_request_budget(1)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices": [{"message": {"content": "ok"}}]}'
        with patch("urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(self.client.chat("system", "question"), "ok")
            self.assertFalse(self.client.request_budget_exhausted)
            with self.assertRaises(ModelRequestBudgetExceeded):
                self.client.chat("system", "second question")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.client.request_count, 1)


if __name__ == "__main__":
    unittest.main()
