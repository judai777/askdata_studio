from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.evaluation import EvaluationService
from app.main import app


class EvalCandidateTest(unittest.TestCase):
    def _baseline(self) -> tuple[EvaluationService, dict, Settings]:
        service = EvaluationService()
        config = Settings(api_key="test-key", llm_model="baseline-chat")
        run = service.run("live", ["guard.restricted_database"], request_limit=20, model_config=config)
        return service, run, config

    def test_candidate_reuses_frozen_experiment_conditions(self) -> None:
        service, baseline, config = self._baseline()
        candidate_config = replace(config, llm_model="candidate-chat")
        with patch("app.evaluation.resolve_model", return_value=candidate_config), patch.object(service._executor, "submit") as submit:
            started = service.start_candidate(baseline["id"], "candidate-chat")
        queued = service.get_run(started["id"])
        self.assertEqual(started["baseline_run_id"], baseline["id"])
        self.assertEqual(queued["task_ids"], baseline["task_ids"])
        self.assertEqual(queued["fixture_hash"], baseline["fixture_hash"])
        self.assertEqual(queued["trials"], baseline["trials"])
        self.assertEqual(queued["request_budget"]["limit"], baseline["request_budget"]["limit"])
        self.assertEqual(queued["grader_hash"], baseline["grader_hash"])
        self.assertEqual(queued["model"], "candidate-chat")
        self.assertEqual(queued["baseline_run_id"], baseline["id"])
        self.assertIs(submit.call_args.args[-2], candidate_config)
        self.assertEqual(submit.call_args.args[-1], baseline["id"])

        submit.call_args.args[0](*submit.call_args.args[1:])
        finished = service.get_run(started["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["baseline_run_id"], baseline["id"])
        self.assertEqual(finished["model"], "candidate-chat")
        self.assertEqual(service.compare(baseline["id"], started["id"])["provenance"]["status"], "changed")
        exported = service.export(started["id"])["run"]
        self.assertEqual(exported["baseline_run_id"], baseline["id"])
        self.assertEqual(exported["task_ids"], baseline["task_ids"])

    def test_rejects_old_or_changed_baseline(self) -> None:
        service, baseline, config = self._baseline()
        candidate_config = replace(config, llm_model="candidate-chat")
        for changed, message in (
            ({"status": "cancelled"}, "已完成"),
            ({"mode": "demo"}, "实时"),
            ({"task_ids": None}, "完整实验条件"),
            ({"grader_hash": "old-grader"}, "判分器已变化"),
            ({"fixture_hash": "old-fixture"}, "数据"),
            ({"provenance": {**baseline["provenance"], "config": {**baseline["provenance"]["config"], "temperature": 0.7}}}, "非 Chat 模型配置"),
            ({"provenance": {**baseline["provenance"], "config": {**baseline["provenance"]["config"], "endpoint_hash": "other-endpoint"}}}, "非 Chat 模型配置"),
        ):
            stale = {**copy.deepcopy(baseline), **changed}
            with self.subTest(changed=changed), patch.object(service, "get_run", return_value=stale), patch("app.evaluation.resolve_model", return_value=candidate_config):
                with self.assertRaisesRegex(ValueError, message):
                    service.start_candidate(baseline["id"], "candidate-chat")

    def test_candidate_endpoint_requires_admin_and_completed_baseline(self) -> None:
        with TestClient(app) as client:
            admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            sales = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
            path = "/api/eval/runs/aaaaaaaaaaaa/candidate"
            self.assertEqual(client.post(path, headers={"Authorization": f"Bearer {sales}"}, json={"model": "candidate-chat"}).status_code, 403)
            self.assertEqual(client.post(path, headers={"Authorization": f"Bearer {admin}"}, json={"model": "candidate-chat"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
