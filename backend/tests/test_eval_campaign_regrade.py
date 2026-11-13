from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import evaluation
from app.eval_campaign import plan_fingerprint, run_campaign
from app.eval_campaign_regrade import regrade_campaign
from app.eval_grader_version import grader_hash
from app.main import app


class CampaignRegradeTests(unittest.TestCase):
    def test_completed_live_run_is_copied_without_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory) / "runs"):
            service = evaluation.EvaluationService()
            root = evaluation.RUN_DIR / "_campaigns"
            source = run_campaign("source000001", ["mock.region_sales", "mock.customer_level"], service=service, directory=root)
            source_path = root / "source000001.json"
            source_run_id = source["shards"][0]["run_ids"][0]
            run_path = evaluation.RUN_DIR / f"{source_run_id}.json"
            original_run = json.loads(run_path.read_text(encoding="utf-8"))
            original_run["mode"] = "live"
            original_run["grader_hash"] = "previous-grader"
            run_path.write_text(json.dumps(original_run), encoding="utf-8")
            source["plan"]["mode"] = "live"
            source["plan"]["grader_hash"] = "previous-grader"
            source["plan_hash"] = plan_fingerprint(source["plan"])
            source_path.write_text(json.dumps(source), encoding="utf-8")
            with patch.object(service, "run", side_effect=AssertionError("model run must not start")):
                derived = regrade_campaign("source000001", "derived00001", directory=root, service=service)
            self.assertEqual(derived["regraded_from"], "source000001")
            self.assertEqual(derived["grader_hash"], grader_hash())
            self.assertEqual(derived["summary"]["passed"], 2)
            derived_run_id = derived["shards"][0]["run_ids"][0]
            derived_run = service.get_run(derived_run_id)
            self.assertEqual(derived_run["regraded_from"], source_run_id)
            self.assertEqual(derived_run["original_grader_hash"], "previous-grader")
            self.assertEqual(derived_run["grader_hash"], grader_hash())
            self.assertEqual(json.loads(run_path.read_text(encoding="utf-8")), original_run)
            self.assertEqual(json.loads(source_path.read_text(encoding="utf-8")), source)
            with self.assertRaisesRegex(ValueError, "已存在"):
                regrade_campaign("source000001", "derived00001", directory=root, service=service)

    def test_rejects_incomplete_and_same_grader_campaigns(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory) / "runs"):
            service = evaluation.EvaluationService()
            root = evaluation.RUN_DIR / "_campaigns"
            record = run_campaign("source000002", ["mock.region_sales"], service=service, directory=root)
            record["plan"]["mode"] = "live"
            record["plan_hash"] = plan_fingerprint(record["plan"])
            path = root / "source000002.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "运行未完成"):
                regrade_campaign("source000002", "derived00002", directory=root, service=service)
            run_path = evaluation.RUN_DIR / f"{record['shards'][0]['run_ids'][0]}.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["mode"] = "live"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "无需重判"):
                regrade_campaign("source000002", "derived00002", directory=root, service=service)
            record["status"] = "failed"
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "已完成"):
                regrade_campaign("source000002", "derived00002", directory=root, service=service)

    def test_regrade_api_requires_admin(self) -> None:
        with TestClient(app) as client:
            sales = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
            response = client.post("/api/eval/campaigns/source000001/regrade",
                                   headers={"Authorization": f"Bearer {sales}"},
                                   json={"campaign_id": "derived00003"})
            self.assertEqual(response.status_code, 403)
