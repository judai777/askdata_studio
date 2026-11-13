from __future__ import annotations

import json
import fcntl
import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import evaluation
from app.eval_campaign import campaign_manager, get_campaign, list_campaigns, main, run_campaign
from app.eval_campaign_gate import approve_baseline, compare_to_baseline
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
                self.assertEqual(resumed["summary"], {"total_tasks": 6, "completed_tasks": 6, "passed": 6, "completed_shards": 2, "total_shards": 2, "remote_requests_used": 0, "remote_requests_uncertain": False})
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

    def test_running_shard_detail_is_not_misclassified_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory) / "runs"):
            root = evaluation.RUN_DIR
            campaign_dir = root / "_campaigns"
            campaign_dir.mkdir(parents=True)
            run_id = "run000000001"
            (root / f"{run_id}.json").write_text(json.dumps({"id": run_id, "status": "running"}), encoding="utf-8")
            (campaign_dir / "demo00000005.json").write_text(json.dumps({"shards": [{"run_ids": [run_id]}]}), encoding="utf-8")
            with (campaign_dir / "demo00000005.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    self.assertEqual(evaluation.EvaluationService().get_run(run_id)["status"], "running")
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
            self.assertEqual(evaluation.EvaluationService().get_run(run_id)["status"], "interrupted")

    def test_live_requires_explicit_remote_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "显式允许远程"):
                run_campaign("live00000001", [TASK_IDS[0]], mode="live", request_limit=10, directory=Path(directory))

    def test_legacy_interrupted_live_run_cannot_resume_with_unknown_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.evaluation.RUN_DIR", root / "runs"):
                record = run_campaign("demo00000006", [TASK_IDS[0]], directory=root / "campaigns")
                record["plan"]["mode"] = "live"
                record["plan"]["request_limit"] = 20
                record["status"] = "failed"
                record["shards"][0]["status"] = "failed"
                (root / "campaigns" / "demo00000006.json").write_text(json.dumps(record), encoding="utf-8")
                run_path = evaluation.RUN_DIR / f"{record['shards'][0]['run_ids'][0]}.json"
                run = json.loads(run_path.read_text(encoding="utf-8"))
                run["status"] = "interrupted"
                run["mode"] = "live"
                run["request_budget"] = {"limit": 20, "used": 0, "exhausted": False}
                run_path.write_text(json.dumps(run), encoding="utf-8")
                from app.eval_campaign import _summary
                self.assertTrue(_summary(record, evaluation.EvaluationService())["remote_requests_uncertain"])

    def test_completed_legacy_live_campaign_backfills_stage_usage_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory) / "runs"):
            campaign_dir = evaluation.RUN_DIR / "_campaigns"
            record = run_campaign("demo00000007", [TASK_IDS[0]], directory=campaign_dir)
            campaign_path = campaign_dir / "demo00000007.json"
            record["plan"]["mode"] = "live"
            campaign_path.write_text(json.dumps(record), encoding="utf-8")
            run_path = evaluation.RUN_DIR / f"{record['shards'][0]['run_ids'][0]}.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["mode"] = "live"
            run["cases"][0]["attempts"][0]["model_calls"] = [{"stage": "preprocess", "success": True, "usage": {"total_tokens": 12}}]
            run_path.write_text(json.dumps(run), encoding="utf-8")
            self.assertEqual(get_campaign("demo00000007", campaign_dir)["summary"]["stage_usage"]["total_tokens"], 12)
            self.assertNotIn("stage_usage", json.loads(campaign_path.read_text(encoding="utf-8"))["summary"])

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

    def test_admin_starts_background_campaign_and_live_requires_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.RUN_DIR", Path(directory) / "runs"):
            with TestClient(app) as client:
                admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
                sales = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
                payload = {"campaign_id": "demo00000004", "task_ids": TASK_IDS, "mode": "demo", "trials": 1}
                self.assertEqual(client.post("/api/eval/campaigns", headers={"Authorization": f"Bearer {sales}"}, json=payload).status_code, 403)
                started = client.post("/api/eval/campaigns", headers={"Authorization": f"Bearer {admin}"}, json=payload)
                self.assertEqual(started.status_code, 200)
                self.assertIn(started.json()["status"], {"queued", "running", "completed"})
                for _ in range(40):
                    progress = client.get("/api/eval/campaigns/demo00000004", headers={"Authorization": f"Bearer {admin}"}).json()
                    if progress["status"] == "completed":
                        break
                    time.sleep(0.1)
                self.assertEqual(progress["summary"]["completed_tasks"], 6)
                self.assertEqual(progress["status"], "completed")
                for _ in range(20):
                    if campaign_manager._active_id is None:
                        break
                    time.sleep(0.05)
                repeated = client.post("/api/eval/campaigns", headers={"Authorization": f"Bearer {admin}"}, json=payload)
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(repeated.json()["shards"], progress["shards"])
                live = {**payload, "campaign_id": "live00000004", "task_ids": [TASK_IDS[0]], "mode": "live", "request_limit": 20, "allow_remote": True}
                with patch("app.eval_campaign.resolve_model", return_value=type("NoKey", (), {"api_key": ""})()):
                    self.assertEqual(client.post("/api/eval/campaigns", headers={"Authorization": f"Bearer {admin}"}, json=live).status_code, 400)

    def test_approved_live_baseline_gate_detects_regression_and_incomplete_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.evaluation.RUN_DIR", root / "runs"):
                service = evaluation.EvaluationService()
                campaign_dir = evaluation.RUN_DIR / "_campaigns"
                for campaign_id, model in (("base00000001", "model-a"), ("cand00000001", "model-b")):
                    record = run_campaign(campaign_id, TASK_IDS[:2], service=service, directory=campaign_dir)
                    run_path = evaluation.RUN_DIR / f"{record['shards'][0]['run_ids'][0]}.json"
                    run = json.loads(run_path.read_text(encoding="utf-8"))
                    run["mode"] = "live"
                    run["provenance"] = {"kind": "live", "prompt_hash": "prompt", "workflow_hash": "flow", "config": {"llm_model": model, "embedding_model": "same"}}
                    if model == "model-b":
                        run["cases"][0]["grade"]["passed"] = False
                    run_path.write_text(json.dumps(run), encoding="utf-8")
                    record["plan"]["mode"] = "live"
                    record["plan"]["request_limit"] = 20
                    record["plan"]["provenance"] = {"kind": "live", "prompt_hash": "prompt", "workflow_hash": "flow", "config": {"llm_model": model, "embedding_model": "same"}}
                    (campaign_dir / f"{campaign_id}.json").write_text(json.dumps(record), encoding="utf-8")
                source = json.loads((campaign_dir / "base00000001.json").read_text(encoding="utf-8"))
                with self.assertRaisesRegex(ValueError, "计划已变化"):
                    approve_baseline("base00000001", "invalid", "verified", "demo_admin", directory=campaign_dir, service=service)
                baseline = approve_baseline("base00000001", source["plan_hash"], "逐题检查", "demo_admin", directory=campaign_dir, service=service)
                self.assertEqual(baseline["passed"], 2)
                with self.assertRaisesRegex(ValueError, "已批准"):
                    approve_baseline("base00000001", source["plan_hash"], "再次批准", "demo_admin", directory=campaign_dir, service=service)
                result = compare_to_baseline("base00000001", "cand00000001", directory=campaign_dir, service=service)
                self.assertFalse(result["passed"])
                self.assertEqual(result["regressed"], [TASK_IDS[0]])
                self.assertEqual(result["variable"], "chat_model")
                candidate_path = campaign_dir / "cand00000001.json"
                candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
                candidate_run_path = evaluation.RUN_DIR / f"{candidate['shards'][0]['run_ids'][0]}.json"
                candidate_run = json.loads(candidate_run_path.read_text(encoding="utf-8"))
                original_candidate = json.dumps(candidate)
                original_candidate_run = json.dumps(candidate_run)
                candidate["plan"]["prompt_variant"] = "time_filter_v1"
                candidate["plan"]["provenance"]["prompt_variant"] = "time_filter_v1"
                candidate["plan"]["provenance"]["prompt_hash"] = "new-prompt"
                candidate["plan"]["provenance"]["config"]["llm_model"] = "model-a"
                candidate_run["provenance"] = candidate["plan"]["provenance"]
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                candidate_run_path.write_text(json.dumps(candidate_run), encoding="utf-8")
                prompt_result = compare_to_baseline("base00000001", "cand00000001", directory=campaign_dir, service=service)
                self.assertEqual(prompt_result["variable"], "prompt")
                candidate["plan"]["provenance"]["config"]["llm_model"] = "model-b"
                candidate_run["provenance"] = candidate["plan"]["provenance"]
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                candidate_run_path.write_text(json.dumps(candidate_run), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "不能同时改变"):
                    compare_to_baseline("base00000001", "cand00000001", directory=campaign_dir, service=service)
                candidate["plan"]["provenance"]["config"]["llm_model"] = "model-a"
                candidate["plan"]["provenance"]["workflow_hash"] = "changed-workflow"
                candidate_run["provenance"] = candidate["plan"]["provenance"]
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                candidate_run_path.write_text(json.dumps(candidate_run), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "工作流版本"):
                    compare_to_baseline("base00000001", "cand00000001", directory=campaign_dir, service=service)
                candidate["plan"]["provenance"]["workflow_hash"] = "flow"
                candidate_path.write_text(original_candidate, encoding="utf-8")
                candidate_run_path.write_text(original_candidate_run, encoding="utf-8")
                candidate = json.loads(original_candidate)
                with TestClient(app) as client:
                    admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
                    sales = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
                    self.assertEqual(client.get("/api/eval/campaign-baselines", headers={"Authorization": f"Bearer {sales}"}).status_code, 403)
                    self.assertEqual(client.post("/api/eval/campaign-baselines", headers={"Authorization": f"Bearer {sales}"}, json={"campaign_id": "base00000001", "fingerprint": source["plan_hash"], "note": "unauthorized"}).status_code, 403)
                    self.assertEqual(client.get("/api/eval/campaign-baselines", headers={"Authorization": f"Bearer {admin}"}).json()[0]["campaign_id"], "base00000001")
                    self.assertEqual(client.get("/api/eval/campaign-gate?baseline=base00000001&candidate=cand00000001", headers={"Authorization": f"Bearer {admin}"}).json(), result)
                candidate["status"] = "interrupted"
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "已完成"):
                    compare_to_baseline("base00000001", "cand00000001", directory=campaign_dir, service=service)


if __name__ == "__main__":
    unittest.main()
