from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.model_client import ModelClient


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


if __name__ == "__main__":
    unittest.main()
