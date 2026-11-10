from __future__ import annotations

import json
import fcntl
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import evaluation
from app.eval_campaign import get_campaign, list_campaigns, main, run_campaign
from app.main import app


TASK_IDS = [
    "mock.region_sales", "mock.customer_level", "mock.target_comparison",
    "route.greeting", "mock.date_range", "metric.paid_vs_list",
]


class EvalCampaignTests(unittest.TestCase):
    def test_demo_campaign_chunks_and_resume_skips_completed_shards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.evaluation.RUN_DIR", root / "runs"):
                service = evaluation.EvaluationService()
                campaign_dir = root / "campaigns"
                actual_run = service.run
                calls = 0

                def fail_second_shard(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("simulated process interruption")
                    return actual_run(*args, **kwargs)

                with patch.object(service, "run", side_effect=fail_second_shard):
                    with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
                        run_campaign("demo00000001", TASK_IDS, service=service, directory=campaign_dir)
                saved = json.loads((campaign_dir / "demo00000001.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["summary"]["completed_tasks"], 5)
                first_run_id = saved["shards"][0]["run_ids"][0]
                resumed = run_campaign("demo00000001", TASK_IDS, service=service, directory=campaign_dir)
                self.assertEqual(resumed["status"], "completed")
                self.assertEqual(resumed["summary"], {"total_tasks": 6, "completed_tasks": 6, "passed": 6, "completed_shards": 2, "total_shards": 2, "remote_requests_used": 0})
                self.assertEqual(resumed["shards"][0]["run_ids"], [first_run_id])
                self.assertEqual(len(resumed["shards"][1]["run_ids"]), 2)
                repeated = run_campaign("demo00000001", TASK_IDS, service=service, directory=campaign_dir)
                self.assertEqual(repeated["shards"], resumed["shards"])
                saved = json.loads((campaign_dir / "demo00000001.json").read_text(encoding="utf-8"))
                saved["status"] = "running"
                saved["shards"][1]["status"] = "running"
                (campaign_dir / "demo00000001.json").write_text(json.dumps(saved), encoding="utf-8")
                reconciled = run_campaign("demo00000001", TASK_IDS, service=service, directory=campaign_dir)
                self.assertEqual(reconciled["status"], "completed")
                self.assertEqual(reconciled["shards"], resumed["shards"])
                with self.assertRaisesRegex(ValueError, "新作业 ID"):
                    run_campaign("demo00000001", TASK_IDS[::-1], service=service, directory=campaign_dir)
                self.assertNotIn("SELECT", json.dumps(repeated))

    def test_same_campaign_cannot_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "demo00000002.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaisesRegex(RuntimeError, "另一进程"):
                        run_campaign("demo00000002", [TASK_IDS[0]], directory=Path(directory))
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def test_live_requires_explicit_remote_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "显式允许远程"):
                run_campaign("live00000001", [TASK_IDS[0]], mode="live", request_limit=10, directory=Path(directory))

    def test_cli_reads_versioned_task_file(self) -> None:
        task_file = Path(__file__).parents[1] / "evals" / "campaigns" / "ci_smoke.json"
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory) / "runs"):
            with patch("sys.argv", ["eval_campaign", "--campaign-id", "ci0000000001", "--task-file", str(task_file)]):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(), 0)
                summary = json.loads(output.getvalue())
                self.assertEqual(summary["summary"]["completed_tasks"], 6)

    def test_admin_can_read_safe_campaign_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.evaluation.RUN_DIR", root / "runs"):
                service = evaluation.EvaluationService()
                run_campaign("demo00000003", [TASK_IDS[0]], service=service)
                campaign = get_campaign("demo00000003")
                self.assertEqual(campaign["summary"]["completed_tasks"], 1)
                self.assertEqual(len(list_campaigns()), 1)
                self.assertNotIn("provenance", json.dumps(campaign))
                self.assertNotIn("SELECT", json.dumps(campaign))
                with TestClient(app) as client:
                    admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
                    sales = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
                    headers = {"Authorization": f"Bearer {admin}"}
                    listed = client.get("/api/eval/campaigns", headers=headers)
                    self.assertEqual(listed.status_code, 200)
                    self.assertEqual(listed.json()[0]["id"], "demo00000003")
                    self.assertEqual(client.get("/api/eval/campaigns/demo00000003", headers=headers).json(), campaign)
                    self.assertEqual(client.get("/api/eval/campaigns/bad", headers=headers).status_code, 404)
                    self.assertEqual(client.get("/api/eval/campaigns", headers={"Authorization": f"Bearer {sales}"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
