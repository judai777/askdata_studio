from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import eval_labels
from app.evaluation import EvaluationService
from app.main import app


class EvalLabelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.label_file = Path(self.temporary.name) / "labels.json"
        patcher = patch.object(eval_labels, "LABEL_FILE", self.label_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_label_history_validation_and_demo_exclusion(self) -> None:
        service = EvaluationService()
        run = service.run("demo", ["mock.region_sales"])
        item = service.labels(run["id"])["items"][0]
        labels = {name: "pass" for name in eval_labels.LABEL_DIMENSIONS}
        with self.assertRaisesRegex(RuntimeError, "已变化"):
            service.submit_label(run["id"], item["task_id"], item["trial"], "old", labels, "依据", "admin")
        with self.assertRaisesRegex(ValueError, "四个维度"):
            service.submit_label(run["id"], item["task_id"], item["trial"], item["fingerprint"], {"completion": "pass"}, "依据", "admin")
        with self.assertRaisesRegex(ValueError, "判断依据"):
            service.submit_label(run["id"], item["task_id"], item["trial"], item["fingerprint"], labels, "", "admin")
        with self.assertRaises(KeyError):
            service.submit_label(run["id"], "missing", 1, item["fingerprint"], labels, "依据", "admin")

        service.submit_label(run["id"], item["task_id"], item["trial"], item["fingerprint"], labels, "第一次", "admin")
        service.submit_label(run["id"], item["task_id"], item["trial"], item["fingerprint"], {**labels, "completion": "fail"}, "第二次", "admin")
        current = service.labels(run["id"])["items"][0]
        self.assertEqual(current["status"], "valid")
        self.assertEqual(len(current["history"]), 2)
        self.assertEqual(current["latest"]["labels"]["completion"], "fail")
        self.assertNotIn(run["cases"][0]["query"], self.label_file.read_text(encoding="utf-8"))
        summary = eval_labels.calibration_summary([run])
        self.assertEqual(summary["demo_practice"], 1)
        self.assertEqual(summary["labeled_attempts"], 0)
        self.assertEqual(summary["groups"], [])

    def test_live_confusion_counts_and_stale_label(self) -> None:
        service = EvaluationService()
        demo = service.run("demo", ["mock.region_sales"], trials=3)
        run = copy.deepcopy(demo)
        run["id"] = "calibration01"
        run["mode"] = "live"
        run["grader_hash"] = "grader-v1"
        task_id = run["cases"][0]["task_id"]
        attempts = run["cases"][0]["attempts"]
        attempts[0]["grade"]["dimensions"] = {"completion": True, "safety": True, "process": True}
        attempts[1]["grade"]["dimensions"] = {"completion": False, "safety": False, "process": False}
        first = eval_labels.attempt_fingerprint(run, task_id, attempts[0])
        second = eval_labels.attempt_fingerprint(run, task_id, attempts[1])
        eval_labels.submit_label(run, task_id, 1, first, {"completion": "fail", "safety": "pass", "process": "unsure", "report_quality": "fail"}, "结果不符", "admin")
        eval_labels.submit_label(run, task_id, 2, second, {"completion": "pass", "safety": "fail", "process": "fail", "report_quality": "unsure"}, "另一次复核", "admin")
        summary = eval_labels.calibration_summary([run])
        self.assertEqual(summary["live_attempts"], 3)
        self.assertEqual(summary["labeled_attempts"], 2)
        group = summary["groups"][0]
        self.assertEqual(group["grader_hash"], "grader-v1")
        self.assertEqual(group["dimensions"]["completion"]["fp"], 1)
        self.assertEqual(group["dimensions"]["completion"]["fn"], 1)
        self.assertEqual(group["dimensions"]["completion"]["agreement"], 0)
        self.assertEqual(group["dimensions"]["safety"]["tp"], 1)
        self.assertEqual(group["dimensions"]["safety"]["tn"], 1)
        self.assertEqual(group["dimensions"]["process"]["unsure"], 1)
        self.assertEqual(group["report_quality"], {"pass": 0, "fail": 1, "unsure": 1})

        run["cases"][0]["attempts"][0]["result"] = {"status": "changed"}
        stale = eval_labels.calibration_summary([run])
        self.assertEqual(stale["stale_labels"], 1)
        self.assertEqual(stale["labeled_attempts"], 1)

    def test_api_requires_admin_and_accepts_label(self) -> None:
        service = EvaluationService()
        run = service.run("demo", ["mock.region_sales"])
        with TestClient(app) as client:
            admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            sales = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
            path = f"/api/eval/runs/{run['id']}/labels"
            self.assertEqual(client.get(path, headers={"Authorization": f"Bearer {sales}"}).status_code, 403)
            item = client.get(path, headers={"Authorization": f"Bearer {admin}"}).json()["items"][0]
            payload = {"fingerprint": item["fingerprint"], "labels": {name: "unsure" for name in eval_labels.LABEL_DIMENSIONS}, "note": "人工待复核"}
            submit_path = f"{path}/{item['task_id']}/{item['trial']}"
            self.assertEqual(client.post(submit_path, headers={"Authorization": f"Bearer {sales}"}, json=payload).status_code, 403)
            self.assertEqual(client.post(submit_path, headers={"Authorization": f"Bearer {admin}"}, json=payload).status_code, 200)
            self.assertEqual(client.get("/api/eval/calibration", headers={"Authorization": f"Bearer {sales}"}).status_code, 403)
            calibration = client.get("/api/eval/calibration", headers={"Authorization": f"Bearer {admin}"})
            self.assertEqual(calibration.status_code, 200)
            self.assertGreaterEqual(calibration.json()["demo_practice"], 1)


if __name__ == "__main__":
    unittest.main()
