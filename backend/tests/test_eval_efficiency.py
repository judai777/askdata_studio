from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.eval_efficiency import stage_usage
from app.evaluation import EvaluationService


class EfficiencyTests(unittest.TestCase):
    def test_complete_usage_sums_stage_tokens_and_shares(self) -> None:
        attempts = [{"model_calls": [
            {"stage": "preprocess", "success": True, "usage": {"total_tokens": 100}},
            {"stage": "retrieve_schema", "success": True, "usage": {"input_tokens": 150, "output_tokens": 50}},
        ]}]
        result = stage_usage(attempts)
        self.assertEqual(result["total_tokens"], 300)
        self.assertEqual(sum(item["known_tokens"] for item in result["stages"]), 300)
        self.assertAlmostEqual(sum(item["token_share"] for item in result["stages"]), 1.0)

    def test_missing_usage_is_reported_as_incomplete_and_old_run_is_backfilled(self) -> None:
        attempts = [{"model_calls": [
            {"stage": "prepare_single_database", "success": True, "usage": {"total_tokens": 80}},
            {"stage": "prepare_single_database", "success": False, "usage": None},
        ]}]
        result = stage_usage(attempts)
        self.assertEqual((result["calls"], result["usage_calls"], result["known_tokens"]), (2, 1, 80))
        self.assertIsNone(result["total_tokens"])
        self.assertIsNone(result["stages"][0]["token_share"])
        retried = stage_usage([{"model_calls": [{"stage": "preprocess", "success": True,
                                                   "attempts": 2, "usage": {"total_tokens": 30}}]}])
        self.assertEqual(retried["retry_requests"], 1)
        self.assertIsNone(retried["total_tokens"])
        untracked = stage_usage(attempts, request_count=3)
        self.assertEqual(untracked["unattributed_requests"], 1)
        self.assertIsNone(untracked["total_tokens"])
        self.assertEqual(stage_usage([], request_count=1)["unattributed_requests"], 1)
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory)):
            path = Path(directory) / "oldrun000001.json"
            path.write_text(json.dumps({"id": "oldrun000001", "mode": "live", "status": "completed",
                                        "summary": {}, "cases": [{"attempts": attempts}]}), encoding="utf-8")
            self.assertEqual(EvaluationService().get_run("oldrun000001")["summary"]["stage_usage"], result)
            self.assertNotIn("stage_usage", json.loads(path.read_text(encoding="utf-8"))["summary"])


if __name__ == "__main__":
    unittest.main()
