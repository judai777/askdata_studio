from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.eval_models import allowed_models, resolve_model
from app.evaluation import EvaluationService
from app.main import app


class EvalModelTest(unittest.TestCase):
    def test_allowlist_and_mode_validation(self) -> None:
        with patch.dict("os.environ", {"EVAL_LLM_MODELS": "candidate-a,candidate-b,candidate-a"}):
            self.assertEqual(allowed_models(), list(dict.fromkeys([settings.llm_model, "candidate-a", "candidate-b"])))
            chosen = resolve_model("live", "candidate-b")
            self.assertEqual(chosen.llm_model, "candidate-b")
            self.assertEqual(chosen.embedding_model, settings.embedding_model)
            self.assertEqual(chosen.api_key, settings.api_key)
            with self.assertRaisesRegex(ValueError, "未登记"):
                resolve_model("live", "other-model")
            with self.assertRaisesRegex(ValueError, "离线演示"):
                resolve_model("demo", "candidate-a")
        with patch.dict("os.environ", {"EVAL_LLM_MODELS": "https://untrusted.example"}):
            with self.assertRaisesRegex(ValueError, "合法模型名"):
                allowed_models()

    def test_candidate_run_uses_isolated_model_and_records_it(self) -> None:
        config = Settings(api_key="test-key", llm_model="candidate-a")
        with patch("app.evaluation.resolve_model", return_value=config):
            run = EvaluationService().run("live", ["guard.restricted_database"], model="candidate-a")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["model"], "candidate-a")
        self.assertEqual(run["provenance"]["config"]["llm_model"], "candidate-a")
        self.assertEqual(settings.llm_model, resolve_model("live", None).llm_model)

    def test_queued_run_freezes_selected_model(self) -> None:
        config = Settings(api_key="test-key", llm_model="candidate-a")
        service = EvaluationService()
        with patch("app.evaluation.resolve_model", return_value=config), patch.object(service._executor, "submit") as submit:
            queued = service.start("live", ["guard.restricted_database"], model="candidate-a")
        self.assertEqual(service.get_run(queued["id"])["model"], "candidate-a")
        self.assertEqual(len(service.get_run(queued["id"])["grader_hash"]), 16)
        self.assertIs(submit.call_args.args[-1], config)

    def test_api_lists_models_and_rejects_unregistered_choice(self) -> None:
        with patch.dict("os.environ", {"EVAL_LLM_MODELS": "candidate-a"}), TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            catalog = client.get("/api/eval/models", headers=headers)
            self.assertEqual(catalog.status_code, 200)
            self.assertIn("candidate-a", catalog.json()["models"])
            payload = {"mode": "live", "task_ids": ["guard.restricted_database"], "model": "other-model"}
            self.assertEqual(client.post("/api/eval/runs/start", headers=headers, json=payload).status_code, 400)
            self.assertEqual(client.post("/api/eval/runs/start", headers=headers, json={**payload, "mode": "demo", "model": "candidate-a"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
