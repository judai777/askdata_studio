from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.eval_models import allowed_models, resolve_model
from app.eval_prompt_variants import prompt_variant_text
from app.eval_provenance import provenance_snapshot
from app.evaluation import EvaluationService
from app.main import app
from app.querying.single_database_agent import SingleDatabaseAgent


class EvalModelTest(unittest.TestCase):
    def test_prompt_variant_only_changes_single_database_agent_system_message(self) -> None:
        systems: list[str] = []
        payloads: list[dict] = []
        def decide(system: str, payload: str) -> dict:
            systems.append(system)
            payloads.append(json.loads(payload))
            return {"action": "call_tool", "tool_name": "query_askdata_mock", "arguments": {}}
        model = SimpleNamespace(chat_json=decide)
        mcp = SimpleNamespace(list_tools=lambda: [{"name": "query_askdata_mock"}], call_tool=lambda *_args: {"success": True})
        skill = SimpleNamespace(instructions="基础规则", allowed_tools=["query_*"], max_tool_calls=3, output_actions=["call_tool"])
        retrieval = {"threshold": 0.5, "hits": [{"doc_id": "orders.order_date", "vector_text": "large duplicate"},
                                                 {"doc_id": "orders.status", "vector_text": "other duplicate"}],
                     "low_confidence_candidates": [{"doc_id": "orders.amount"}]}
        graph = {"fields": [{"id": "orders.order_date", "name": "order_date"}], "joins": []}
        for variant in ("default", "time_filter_v1", "compact_context_v1"):
            agent = SingleDatabaseAgent(model, lambda _scope: mcp, skill, prompt_variant=variant)
            self.assertEqual(agent.prepare("统计销售额", "askdata_mock", graph, "字段说明", retrieval, {}, {})["action"], "executed")
        self.assertNotIn("逐项核对", systems[0])
        self.assertIn("逐项核对", systems[1])
        self.assertIn("selected_field_ids", systems[2])
        self.assertEqual(payloads[0]["retrieval"]["selected_fields"], retrieval["hits"])
        self.assertEqual(payloads[1]["retrieval"]["selected_fields"], retrieval["hits"])
        self.assertEqual(payloads[2]["retrieval"]["selected_field_ids"], [hit["doc_id"] for hit in retrieval["hits"]])
        self.assertEqual(payloads[2]["retrieval"]["low_confidence_candidates"], retrieval["low_confidence_candidates"])
        self.assertEqual(payloads[2]["schema_graph"], graph)
        self.assertEqual(payloads[2]["schema_text"], "字段说明")
        self.assertEqual(payloads[2]["mcp_tools"], payloads[0]["mcp_tools"])

    def test_allowlist_and_mode_validation(self) -> None:
        with patch.dict("os.environ", {"EVAL_LLM_MODELS": "candidate-a,candidate-b,candidate-a"}):
            self.assertEqual(allowed_models(), list(dict.fromkeys([settings.llm_model, "candidate-a", "candidate-b"])))
            chosen = resolve_model("live", "candidate-b")
            self.assertEqual(chosen.llm_model, "candidate-b")
            self.assertEqual(chosen.embedding_model, settings.embedding_model)
            self.assertEqual(chosen.api_key, settings.api_key)
            prompted = resolve_model("live", "candidate-b", "time_filter_v1")
            self.assertEqual(prompted.eval_prompt_variant, "time_filter_v1")
            self.assertNotEqual(provenance_snapshot("live", chosen)["prompt_hash"], provenance_snapshot("live", prompted)["prompt_hash"])
            self.assertIn("时间范围", prompt_variant_text("time_filter_v1"))
            with self.assertRaisesRegex(ValueError, "Prompt 版本"):
                resolve_model("live", "candidate-b", "unknown")
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

    def test_default_chat_model_still_uses_selected_prompt_variant(self) -> None:
        config = Settings(api_key="test-key", llm_model=settings.llm_model, eval_prompt_variant="time_filter_v1")
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory)):
            run = EvaluationService().run("live", ["guard.restricted_database"], request_limit=1, model_config=config)
        self.assertEqual(run["provenance"]["prompt_variant"], "time_filter_v1")
        self.assertEqual(run["request_budget"]["used"], 0)

    def test_queued_run_freezes_selected_model(self) -> None:
        config = Settings(api_key="test-key", llm_model="candidate-a")
        service = EvaluationService()
        with patch("app.evaluation.resolve_model", return_value=config), patch.object(service._executor, "submit") as submit:
            queued = service.start("live", ["guard.restricted_database"], model="candidate-a")
        self.assertEqual(service.get_run(queued["id"])["model"], "candidate-a")
        self.assertEqual(len(service.get_run(queued["id"])["grader_hash"]), 16)
        self.assertIs(submit.call_args.args[-2], config)
        self.assertIsNone(submit.call_args.args[-1])

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
