from __future__ import annotations

import unittest
import time
import json
import tempfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.evaluation import EvaluationService, _fixture_hash, aggregate_metrics, aggregate_stages, attribute_failure, attempt_metrics, combine_attempts, compare_records, failure_summary, fixture_engine, grade, oracle_sql, task_catalog
from app.eval_provenance import provenance_snapshot
from app.eval_export import export_report
from app.model_client import ModelRequestBudgetExceeded
from app.config import Settings
from app.main import app


class EvaluationTest(unittest.TestCase):
    def test_demo_and_replay(self) -> None:
        service = EvaluationService()
        ids = ["mock.date_range", "mock.clarify_orders", "guard.restricted_database"]
        run = service.run("demo", ids, trials=3)
        self.assertEqual(run["summary"]["passed"], len(ids))
        self.assertEqual(run["summary"]["pass3"], 2)
        self.assertEqual(run["summary"]["metrics"]["model_calls"], 0)
        self.assertEqual(run["provenance"]["kind"], "demo")
        self.assertIsNone(run["provenance"]["prompt_hash"])
        self.assertEqual(service.get_run(run["id"], replay=True)["summary"], run["summary"])

    def test_export_report_uses_field_allowlist_and_preserves_missing_metrics(self) -> None:
        service = EvaluationService()
        run = service.run("demo", ["mock.region_sales"])
        report = service.export(run["id"])
        self.assertEqual(report["format_version"], "1.0")
        self.assertEqual(report["run"]["fixture_hash"], run["fixture_hash"])
        self.assertEqual(report["run"]["oracle_hash"], run["oracle_hash"])
        self.assertIsNone(report["summary"]["stage_metrics"])
        self.assertIsNone(report["cases"][0]["attempts"][0]["stage_metrics"])
        self.assertNotIn("result", report["cases"][0])
        self.assertNotIn("query", report["cases"][0])

        contaminated = json.loads(json.dumps(run))
        contaminated["secret"] = "secret-top-level"
        contaminated["provenance"] = {"kind": "live", "config": {"llm_model": "test-model", "api_key": "secret-key", "llm_base_url": "secret-url"}}
        contaminated["cases"][0]["result"] = {"sql": "secret-sql", "rows": [{"password": "secret-row"}]}
        contaminated["cases"][0]["attempts"][0]["error"] = "secret-error"
        contaminated["cases"][0]["metrics"]["elapsed_ms"] = "secret-metric"
        contaminated["cases"][0]["attempts"][0]["stages"] = [{"stage": "preprocess", "elapsed_ms": 2.5, "success": False, "failure_type": "secret-failure"}]
        exported = export_report(contaminated)
        serialized = json.dumps(exported)
        for secret in ("secret-top-level", "secret-key", "secret-url", "secret-sql", "secret-row", "secret-error", "secret-failure", "secret-metric"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(exported["cases"][0]["attempts"][0]["stage_metrics"][0]["elapsed_ms"], 2.5)
        with self.assertRaisesRegex(ValueError, "只能导出"):
            export_report({**run, "status": "running"})
        cancelled = export_report({**run, "status": "cancelled", "cases": [{**run["cases"][0], "incomplete": True}]})
        self.assertTrue(cancelled["cases"][0]["incomplete"])

    def test_export_api_requires_admin_and_finished_run(self) -> None:
        with TestClient(app) as client:
            admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            sales = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
            admin_headers = {"Authorization": f"Bearer {admin}"}
            sales_headers = {"Authorization": f"Bearer {sales}"}
            run = client.post("/api/eval/runs", headers=admin_headers, json={"mode": "demo", "task_ids": ["mock.region_sales"]}).json()
            path = f"/api/eval/runs/{run['id']}/export"
            self.assertEqual(client.get(path, headers=sales_headers).status_code, 403)
            response = client.get(path, headers=admin_headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["run"]["id"], run["id"])
            self.assertEqual(client.get("/api/eval/runs/aaaaaaaaaaaa/export", headers=admin_headers).status_code, 404)
            with patch.object(EvaluationService, "export", side_effect=ValueError("只能导出已完成或已取消的运行")):
                self.assertEqual(client.get(path, headers=admin_headers).status_code, 409)

    def test_live_provenance_changes_with_sources_and_excludes_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.py"
            workflow = Path(directory) / "workflow.py"
            prompt.write_text("first prompt", encoding="utf-8")
            workflow.write_text("first workflow", encoding="utf-8")
            config = Settings(api_key="test-secret-key", llm_base_url="https://private.example", llm_model="test-chat")
            with patch("app.eval_provenance.PROMPT_SOURCES", (str(prompt),)), patch("app.eval_provenance.WORKFLOW_SOURCES", (str(workflow),)):
                original = provenance_snapshot("live", config)
                prompt.write_text("second prompt", encoding="utf-8")
                prompt_changed = provenance_snapshot("live", config)
                workflow.write_text("second workflow", encoding="utf-8")
                workflow_changed = provenance_snapshot("live", config)
        self.assertNotEqual(original["prompt_hash"], prompt_changed["prompt_hash"])
        self.assertEqual(original["workflow_hash"], prompt_changed["workflow_hash"])
        self.assertNotEqual(prompt_changed["workflow_hash"], workflow_changed["workflow_hash"])
        self.assertEqual(prompt_changed["prompt_hash"], workflow_changed["prompt_hash"])
        self.assertEqual(original["config"]["llm_model"], "test-chat")
        self.assertNotEqual(original["config"]["endpoint_hash"], provenance_snapshot("live", Settings(api_key="test-secret-key", llm_base_url="https://other.example", llm_model="test-chat"))["config"]["endpoint_hash"])
        self.assertNotIn("test-secret-key", json.dumps(original))
        self.assertNotIn("private.example", json.dumps(original))

    def test_live_run_records_provenance_without_model_call(self) -> None:
        config = Settings(api_key="test-secret-key", llm_model="test-chat")
        fake = SimpleNamespace(config=config, model_client=SimpleNamespace(enabled=True, telemetry_enabled=False, telemetry=[], configure_request_budget=lambda _limit: None, request_count=0, request_budget_exhausted=False), workflow=SimpleNamespace(database_engine=None))
        with patch("app.evaluation.AskDataService", return_value=fake):
            run = EvaluationService().run("live", ["guard.restricted_database"])
        self.assertEqual(run["summary"]["passed"], 1)
        self.assertEqual(run["provenance"]["kind"], "live")
        self.assertTrue(run["provenance"]["prompt_hash"])
        self.assertTrue(run["provenance"]["workflow_hash"])
        self.assertNotIn("test-secret-key", json.dumps(run))

    def test_live_run_persists_stage_trace_and_summary(self) -> None:
        config = Settings(api_key="test-secret-key", llm_model="test-chat")
        fake = SimpleNamespace(config=config, model_client=SimpleNamespace(enabled=True, telemetry_enabled=False, telemetry=[], configure_request_budget=lambda _limit: None, request_count=0, request_budget_exhausted=False), workflow=SimpleNamespace(database_engine=None, stage_telemetry=[]))
        service = EvaluationService()
        verdict = {"passed": False, "checks": {"execution": False}, "dimensions": {"completion": False, "safety": True, "process": False}}
        def execute(*_args, **_kwargs):
            fake.workflow.stage_telemetry.append({"stage": "preprocess", "elapsed_ms": 8.5, "success": False, "failure_type": "model_unavailable_fallback"})
            return {"status": "completed"}
        with patch("app.evaluation.AskDataService", return_value=fake), patch.object(service, "_execute_case", side_effect=execute), patch("app.evaluation.grade", return_value=verdict):
            run = service.run("live", ["mock.region_sales"])
        self.assertTrue(fake.workflow.stage_telemetry_enabled)
        self.assertEqual(run["cases"][0]["attempts"][0]["stages"][0]["stage"], "preprocess")
        self.assertEqual(run["cases"][0]["stage_metrics"][0]["elapsed_ms"], 8.5)
        self.assertEqual(run["summary"]["stage_metrics"][0]["calls"], 1)
        self.assertEqual(run["cases"][0]["failure_attribution"]["stage"], "preprocess")
        self.assertEqual(run["summary"]["failure_summary"], [{"kind": "stage", "stage": "preprocess", "count": 1}])
        self.assertEqual(service.get_run(run["id"], replay=True)["summary"]["stage_metrics"][0]["elapsed_ms"], 8.5)

    def test_remote_request_budget_stops_remaining_tasks_and_keeps_report(self) -> None:
        config = Settings(api_key="test-secret-key", llm_model="test-chat")
        model = SimpleNamespace(enabled=True, telemetry_enabled=False, telemetry=[], request_count=0, request_budget_exhausted=False, configure_request_budget=lambda _limit: None)
        fake = SimpleNamespace(config=config, model_client=model, workflow=SimpleNamespace(database_engine=None, stage_telemetry=[]))
        service = EvaluationService()
        calls = []
        def execute(task, *_args, **_kwargs):
            calls.append(task["id"])
            model.request_count = 1
            model.request_budget_exhausted = True
            raise ModelRequestBudgetExceeded("远程请求预算已耗尽")
        with patch("app.evaluation.AskDataService", return_value=fake), patch.object(service, "_execute_case", side_effect=execute):
            run = service.run("live", ["mock.region_sales", "metric.paid_vs_list"], request_limit=1)
        self.assertEqual(run["status"], "budget_exhausted")
        self.assertEqual(calls, ["mock.region_sales"])
        self.assertEqual(run["request_budget"], {"limit": 1, "used": 1, "exhausted": True})
        self.assertEqual(run["progress"]["completed_attempts"], 1)
        self.assertEqual(len(run["cases"]), 1)
        self.assertEqual(service.export(run["id"])["request_budget"]["used"], 1)
        with self.assertRaisesRegex(ValueError, "不能重放"):
            service.get_run(run["id"], replay=True)

    def test_request_budget_api_validation(self) -> None:
        service = EvaluationService()
        self.assertEqual(service._request_limit("live", None), 50)
        self.assertIsNone(service._request_limit("demo", None))
        for value in (0, 101, True):
            with self.assertRaises(ValueError):
                service._request_limit("live", value)
        with self.assertRaises(ValueError):
            service._request_limit("demo", 20)
        with TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            base = {"mode": "live", "task_ids": ["mock.region_sales"]}
            self.assertEqual(client.post("/api/eval/runs/start", headers=headers, json={**base, "request_limit": 101}).status_code, 400)
            self.assertEqual(client.post("/api/eval/runs/start", headers=headers, json={**base, "request_limit": True}).status_code, 422)

    def test_wrong_result_and_write_sql_fail(self) -> None:
        task = task_catalog()["tasks"][0]
        result = {"route": "database_query", "status": "completed", "sql": "DELETE FROM orders_current", "rows": [{"region": "华东", "sales": 1}], "tool_calls": []}
        verdict = grade(task, result, [{"region": "华东", "sales": 2}])
        self.assertFalse(verdict["passed"])
        self.assertFalse(verdict["dimensions"]["safety"])

    def test_time_and_clarification_require_proof(self) -> None:
        catalog = {task["id"]: task for task in task_catalog()["tasks"]}
        time_task = catalog["mock.date_range"]
        wrong_time = {"route": "database_query", "status": "completed", "sql": "SELECT region, SUM(paid_amount) FROM orders_current GROUP BY region", "rows": [], "tool_calls": []}
        self.assertFalse(grade(time_task, wrong_time, [])["checks"]["time_fields"])
        clarification = catalog["mock.clarify_orders"]
        direct = {"initial": {"status": "completed"}, "resumed": {"route": "database_query", "status": "completed", "sql": oracle_sql(clarification["id"]), "rows": [], "tool_calls": []}}
        self.assertFalse(grade(clarification, direct, [])["checks"]["clarification"])

    def test_security_probe_and_mixed_trials(self) -> None:
        service = EvaluationService()
        task = next(item for item in task_catalog()["tasks"] if item["id"] == "guard.restricted_database")
        response = service._execute_case(task, "demo", None, "eval-probe", [])
        self.assertFalse(response["success"])
        self.assertTrue(grade(task, response, [])["passed"])
        good = {"checks": {"result": True}, "passed": True, "dimensions": {"completion": True, "safety": True, "process": True}}
        bad = {"checks": {"result": False}, "passed": False, "dimensions": {"completion": False, "safety": True, "process": True}}
        self.assertFalse(combine_attempts([{"grade": good}, {"grade": bad}, {"grade": good}])["passed"])

    def test_metric_field_and_new_oracles(self) -> None:
        service = EvaluationService()
        tasks = [task for task in task_catalog()["tasks"] if task["id"].startswith(("metric.", "time."))]
        self.assertEqual(len(tasks), 5)
        for task in tasks:
            self.assertTrue(service._oracle(task), task["id"])
        task = next(item for item in tasks if item["id"] == "metric.paid_vs_list")
        wrong = {"route": "database_query", "status": "completed", "sql": oracle_sql(task["id"]).replace("paid_amount", "order_amount"), "rows": service._oracle(task), "tool_calls": []}
        self.assertFalse(grade(task, wrong, service._oracle(task))["checks"]["metric_fields"])

    def test_new_development_tasks_run_on_all_fixtures(self) -> None:
        service = EvaluationService()
        ids = ["join.customer_orders", "grain.unique_customers", "time.july_august"]
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.REVIEW_RECORD_FILE", Path(directory) / "reviews.json"):
            for fixture_id in ("base", "edge_v1", "edge_v2", "edge_v3"):
                run = service.run("demo", ids, fixture_id=fixture_id)
                self.assertEqual(run["summary"]["passed"], 3)
                self.assertEqual(run["review"], {"total": 3, "approved": 0, "rejected": 0, "pending": 3, "stale": 0})
                self.assertEqual(service.get_run(run["id"], replay=True)["summary"], run["summary"])

    def test_review_state_is_manual_persistent_and_invalidated_by_changes(self) -> None:
        service = EvaluationService()
        task_id = "grain.unique_customers"
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "reviews.json"
            spec_path = Path(directory) / "specs.json"
            with patch("app.evaluation.REVIEW_RECORD_FILE", record_path):
                initial = service.reviews()
                self.assertEqual(initial["counts"]["pending"], 14)
                readiness = service.benchmark_readiness()
                self.assertEqual(readiness["existing"], {"total": 14, "approved": 0, "sql_tasks": 12, "with_field_constraints": 9})
                self.assertEqual(readiness["target"], {"database": "ecommerce_ops", "development": 0, "development_goal": 49, "holdout": 0, "holdout_goal": 381, "total_goal": 430})
                preview = service.review_preview(task_id)
                self.assertTrue(preview["oracle_sql"])
                self.assertEqual(set(preview["expected_rows"]), {"base", "edge_v1", "edge_v2", "edge_v3"})
                fingerprint = preview["fingerprint"]
                submitted = service.submit_review(task_id, "approved", "确认使用去重客户数，三套数据结果正确", fingerprint, "demo_admin")
                self.assertEqual(submitted["status"], "approved")
                self.assertTrue(record_path.exists())
                self.assertEqual(service.review_summary([task_id])["approved"], 1)
                self.assertEqual(service.benchmark_readiness()["existing"]["approved"], 1)
                self.assertEqual(service.review_preview(task_id)["history"][0]["reviewer"], "demo_admin")
                original_specs = json.loads((Path(__file__).parents[1] / "evals" / "review_specs.json").read_text(encoding="utf-8"))
                original_specs["tasks"][task_id]["pitfall"] += "补充审核说明"
                spec_path.write_text(json.dumps(original_specs, ensure_ascii=False), encoding="utf-8")
                with patch("app.evaluation.REVIEW_SPEC_FILE", spec_path):
                    changed = service.reviews()
                    self.assertEqual(next(item for item in changed["tasks"] if item["task_id"] == task_id)["status"], "stale")
                    with self.assertRaisesRegex(RuntimeError, "已变化"):
                        service.submit_review(task_id, "approved", "旧指纹不应有效", fingerprint, "demo_admin")

    def test_edge_fixture_changes_oracle_and_replays(self) -> None:
        service = EvaluationService()
        self.assertEqual(_fixture_hash("base"), "360b511959d6a4c4")
        self.assertEqual(_fixture_hash("edge_v1"), "06d46c2bf6760fde")
        self.assertNotEqual(_fixture_hash("edge_v2"), _fixture_hash("edge_v1"))
        self.assertNotEqual(_fixture_hash("edge_v3"), _fixture_hash("edge_v2"))
        task = next(t for t in task_catalog()["tasks"] if t["id"] == "time.single_day")
        base_rows = service._oracle(task)
        with fixture_engine("edge_v1") as engine:
            edge_rows = service._oracle(task, engine)
            self.assertNotEqual(base_rows, edge_rows)
            wrong_sql = oracle_sql(task["id"]).replace("status = '已支付' AND ", "")
            wrong = engine.execute(task["database"], wrong_sql)
            self.assertTrue(wrong.success)
            self.assertNotEqual(wrong.rows, edge_rows)
        run = service.run("demo", ["time.single_day", "metric.paid_vs_list"], fixture_id="edge_v1")
        self.assertEqual(run["summary"]["passed"], 2)
        self.assertEqual(service.get_run(run["id"], replay=True)["summary"], run["summary"])
        next_run = service.run("demo", ["mock.region_sales", "time.single_day"], fixture_id="edge_v2")
        self.assertEqual(next_run["summary"]["passed"], 2)
        self.assertEqual(service.get_run(next_run["id"], replay=True)["summary"], next_run["summary"])
        with self.assertRaisesRegex(ValueError, "数据指纹"):
            service.compare(run["id"], next_run["id"])
        self.assertTrue(all("sql" not in t for t in service.catalog()["tasks"]))
        with patch("app.evaluation._oracle_hash", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "标准答案"):
                service.get_run(run["id"], replay=True)

    def test_mutant_audit_shows_fixture_gain(self) -> None:
        report = EvaluationService().audit()
        self.assertEqual(report["counts"]["base"], {"killed": 10, "survived": 10, "invalid": 0})
        self.assertEqual(report["counts"]["edge_v1"], {"killed": 13, "survived": 7, "invalid": 0})
        self.assertEqual(report["counts"]["edge_v2"], {"killed": 14, "survived": 6, "invalid": 0})
        self.assertEqual(report["counts"]["edge_v3"], {"killed": 20, "survived": 0, "invalid": 0})
        self.assertEqual(report["counts"]["overall"], {"killed": 20, "survived": 0, "invalid": 0})
        improved = [case for case in report["cases"] if case["fixtures"]["base"]["status"] == "survived" and case["fixtures"]["edge_v1"]["status"] == "killed"]
        self.assertGreaterEqual(len(improved), 2)
        blind_spot = next(case for case in report["cases"] if case["id"] == "region.distinct_amount")
        self.assertEqual(blind_spot["status"], "killed")
        self.assertEqual(blind_spot["fixtures"]["base"]["status"], "survived")
        self.assertEqual(blind_spot["fixtures"]["edge_v1"]["status"], "survived")
        self.assertEqual(blind_spot["fixtures"]["edge_v2"]["status"], "killed")
        self.assertIn("edge_v2", blind_spot["suggestion"])
        month_cases = [case for case in report["cases"] if case["id"].endswith("omit_august_month") or case["id"] == "target.omit_order_month"]
        self.assertEqual(len(month_cases), 6)
        for case in month_cases:
            self.assertTrue(all(case["fixtures"][fixture]["status"] == "survived" for fixture in ("base", "edge_v1", "edge_v2")))
            self.assertEqual(case["fixtures"]["edge_v3"]["status"], "killed")
        self.assertTrue(all(outcome["status"] != "invalid" for case in report["cases"] for outcome in case["fixtures"].values()))
        self.assertTrue(report["mutant_hash"])

    def test_mutant_audit_does_not_count_invalid_sql_as_killed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mutants.json"
            path.write_text(json.dumps({"version": "test", "mutants": [{"id": "bad", "task_id": "mock.region_sales", "fault": "无效语法", "sql": "SELECT FROM orders_current"}]}), encoding="utf-8")
            with patch("app.evaluation.MUTANT_FILE", path):
                report = EvaluationService().audit()
        self.assertEqual(report["counts"]["overall"], {"killed": 0, "survived": 0, "invalid": 1})
        self.assertEqual(report["counts"]["base"]["invalid"], 1)

    def test_eval_api_is_admin_only(self) -> None:
        with TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "sales", "password": "sales123"}).json()["access_token"]
            self.assertEqual(client.get("/api/eval/catalog", headers={"Authorization": f"Bearer {token}"}).status_code, 403)
            self.assertEqual(client.get("/api/eval/audit", headers={"Authorization": f"Bearer {token}"}).status_code, 403)
            self.assertEqual(client.get("/api/eval/reviews", headers={"Authorization": f"Bearer {token}"}).status_code, 403)
            self.assertEqual(client.get("/api/eval/reviews/mock.region_sales/preview", headers={"Authorization": f"Bearer {token}"}).status_code, 403)
            self.assertEqual(client.post("/api/eval/reviews/mock.region_sales", headers={"Authorization": f"Bearer {token}"}, json={"decision": "approved", "note": "无权限", "fingerprint": "x"}).status_code, 403)
            self.assertEqual(client.post("/api/eval/runs/aaaaaaaaaaaa/cancel", headers={"Authorization": f"Bearer {token}"}).status_code, 403)

    def test_admin_api_run_and_replay(self) -> None:
        with TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            response = client.post("/api/eval/runs", headers=headers, json={"mode": "demo", "task_ids": ["mock.region_sales"]})
            self.assertEqual(response.status_code, 200, response.text)
            run = response.json()
            self.assertEqual(run["summary"]["passed"], 1)
            replay = client.post(f"/api/eval/runs/{run['id']}/replay", headers=headers)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["summary"], run["summary"])
            other = client.post("/api/eval/runs", headers=headers, json={"mode": "demo", "task_ids": ["mock.region_sales", "metric.paid_vs_list"]}).json()
            compared = client.get("/api/eval/compare", headers=headers, params={"baseline": run["id"], "candidate": other["id"]})
            self.assertEqual(compared.status_code, 200, compared.text)
            self.assertEqual(compared.json()["counts"]["common"], 1)
            self.assertEqual(compared.json()["provenance"]["status"], "same")
            self.assertEqual(compared.json()["only_candidate"], ["metric.paid_vs_list"])
            catalog = client.get("/api/eval/catalog", headers=headers).json()
            self.assertTrue(all("sql" not in task for task in catalog["tasks"]))
            audit = client.get("/api/eval/audit", headers=headers)
            self.assertEqual(audit.status_code, 200, audit.text)
            self.assertEqual(audit.json()["counts"]["edge_v1"]["killed"], 13)
            self.assertEqual(audit.json()["counts"]["edge_v2"]["killed"], 14)
            self.assertEqual(audit.json()["counts"]["edge_v3"]["killed"], 20)
            edge = client.post("/api/eval/runs", headers=headers, json={"mode": "demo", "task_ids": ["mock.region_sales"], "fixture_id": "edge_v1"})
            self.assertEqual(edge.status_code, 200, edge.text)
            self.assertEqual(edge.json()["fixture_id"], "edge_v1")
            incompatible = client.get("/api/eval/compare", headers=headers, params={"baseline": run["id"], "candidate": edge.json()["id"]})
            self.assertEqual(incompatible.status_code, 409)
            newer = client.post("/api/eval/runs", headers=headers, json={"mode": "demo", "task_ids": ["mock.region_sales"], "fixture_id": "edge_v2"})
            self.assertEqual(newer.status_code, 200, newer.text)
            self.assertEqual(newer.json()["fixture_id"], "edge_v2")
            self.assertEqual(client.post(f"/api/eval/runs/{newer.json()['id']}/replay", headers=headers).status_code, 200)

    def test_admin_review_api_records_human_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.evaluation.REVIEW_RECORD_FILE", Path(directory) / "reviews.json"), TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            listed = client.get("/api/eval/reviews", headers=headers)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json()["counts"]["pending"], 14)
            preview = client.get("/api/eval/reviews/join.customer_orders/preview", headers=headers)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertIn("JOIN customers", preview.json()["oracle_sql"])
            submitted = client.post("/api/eval/reviews/join.customer_orders", headers=headers, json={"decision": "approved", "note": "已核对客户 ID 关联、订单去重和三套预期结果", "fingerprint": preview.json()["fingerprint"]})
            self.assertEqual(submitted.status_code, 200, submitted.text)
            self.assertEqual(submitted.json()["review"]["reviewer"], "demo_admin")
            self.assertEqual(client.get("/api/eval/reviews", headers=headers).json()["counts"]["approved"], 1)
            stale = client.post("/api/eval/reviews/join.customer_orders", headers=headers, json={"decision": "approved", "note": "旧版本", "fingerprint": "outdated"})
            self.assertEqual(stale.status_code, 409)
            run = client.post("/api/eval/runs", headers=headers, json={"mode": "demo", "task_ids": ["join.customer_orders"]})
            self.assertEqual(run.status_code, 200, run.text)
            self.assertEqual(run.json()["review"]["approved"], 1)

    def test_compare_regression_improvement_and_coverage(self) -> None:
        def item(task_id: str, passed: bool) -> dict:
            return {"task_id": task_id, "query": task_id, "grade": {"passed": passed, "checks": {"result": passed, "safety": True}}}

        baseline = {"id": "baseline0001", "status": "completed", "task_version": "3.0", "fixture_hash": "fixed", "mode": "live", "trials": 1, "cases": [item("a", True), item("b", False), item("c", True)]}
        candidate = {**baseline, "id": "candidate001", "cases": [item("a", False), item("b", True), item("d", True)]}
        diff = compare_records(baseline, candidate)
        self.assertEqual(diff["counts"]["regressed"], 1)
        self.assertEqual(diff["counts"]["improved"], 1)
        self.assertEqual(diff["only_baseline"], ["c"])
        self.assertEqual(diff["only_candidate"], ["d"])
        self.assertEqual(diff["cases"][0]["check_changes"], [{"name": "result", "before": True, "after": False}])
        self.assertEqual(diff["provenance"]["status"], "unknown")
        old_snapshot = provenance_snapshot("demo")
        same = compare_records({**baseline, "provenance": old_snapshot}, {**candidate, "provenance": old_snapshot})
        self.assertEqual(same["provenance"]["status"], "same")
        prior_snapshot = {"kind": "live", "prompt_hash": "old-prompt", "workflow_hash": "same-workflow", "config": {"llm_model": "old-model"}}
        changed_snapshot = {"kind": "live", "prompt_hash": "new-prompt", "workflow_hash": "same-workflow", "config": {"llm_model": "new-model"}}
        changed = compare_records({**baseline, "provenance": prior_snapshot}, {**candidate, "provenance": changed_snapshot})
        self.assertEqual(changed["provenance"]["status"], "changed")
        self.assertIn("prompt_hash", [item["field"] for item in changed["provenance"]["changes"]])
        self.assertIn("config.llm_model", [item["field"] for item in changed["provenance"]["changes"]])
        with self.assertRaisesRegex(ValueError, "数据指纹"):
            compare_records(baseline, {**candidate, "fixture_hash": "changed"})
        with self.assertRaisesRegex(ValueError, "试验次数"):
            compare_records(baseline, {**candidate, "trials": 3})
        with self.assertRaisesRegex(ValueError, "标准答案版本"):
            compare_records({**baseline, "oracle_hash": "one"}, {**candidate, "oracle_hash": "two"})

    def test_usage_coverage_and_cost_difference(self) -> None:
        measured = attempt_metrics(1200, [{"success": True, "usage": {"total_tokens": 15}}, {"success": True, "usage": None}])
        self.assertEqual(measured["model_calls"], 2)
        self.assertEqual(measured["usage_calls"], 1)
        self.assertIsNone(measured["total_tokens"])
        complete = attempt_metrics(900, [{"success": True, "usage": {"input_tokens": 5, "output_tokens": 4}}])
        self.assertEqual(complete["total_tokens"], 9)
        self.assertEqual(aggregate_metrics([{"metrics": complete}, {"metrics": measured}])["total_tokens"], None)
        base = {"id": "baseline0001", "status": "completed", "task_version": "3.0", "fixture_hash": "fixed", "mode": "live", "trials": 1, "cases": [{"task_id": "a", "grade": {"passed": True, "checks": {}}, "metrics": complete}]}
        new = {**base, "id": "candidate001", "cases": [{"task_id": "a", "grade": {"passed": True, "checks": {}}, "metrics": measured}]}
        self.assertEqual(compare_records(base, new)["cases"][0]["cost_change"]["elapsed_ms_delta"], 300)
        self.assertIsNone(compare_records(base, new)["cases"][0]["cost_change"]["total_tokens_delta"])
        self.assertEqual(compare_records(base, new)["budget_comparison"]["status"], "unknown")
        before_budget = {**base, "request_budget": {"limit": 20, "used": 10, "exhausted": False}}
        after_budget = {**new, "request_budget": {"limit": 50, "used": 11, "exhausted": False}}
        self.assertEqual(compare_records(before_budget, after_budget)["budget_comparison"], {"status": "changed", "baseline_limit": 20, "candidate_limit": 50})

    def test_stage_aggregation_and_comparison_preserve_unknowns(self) -> None:
        self.assertIsNone(aggregate_stages([]))
        stages = [
            {"stage": "preprocess", "elapsed_ms": 10, "success": True},
            {"stage": "preprocess", "elapsed_ms": 12, "success": False},
            {"stage": "mcp_tool_call", "elapsed_ms": None, "success": False},
        ]
        self.assertEqual(aggregate_stages(stages), [
            {"stage": "mcp_tool_call", "calls": 1, "failures": 1, "elapsed_ms": None},
            {"stage": "preprocess", "calls": 2, "failures": 1, "elapsed_ms": 22},
        ])
        common = {"status": "completed", "task_version": "3.0", "fixture_hash": "fixed", "mode": "live", "trials": 1}
        case = {"task_id": "a", "grade": {"passed": True, "checks": {}}, "stage_metrics": aggregate_stages(stages)}
        before = {**common, "id": "baseline0001", "cases": [case]}
        after_case = {**case, "stage_metrics": [
            {"stage": "mcp_tool_call", "calls": 1, "failures": 0, "elapsed_ms": 5},
            {"stage": "preprocess", "calls": 3, "failures": 0, "elapsed_ms": 25},
        ]}
        after = {**common, "id": "candidate001", "cases": [after_case]}
        changes = compare_records(before, after)["cases"][0]["stage_changes"]
        self.assertEqual(changes[0]["elapsed_ms_delta"], None)
        self.assertEqual(changes[1], {"stage": "preprocess", "elapsed_ms_delta": 3, "calls_delta": 1, "failures_delta": -1})
        self.assertEqual(compare_records({**before, "cases": [{"task_id": "a", "grade": case["grade"]}]}, after)["cases"][0]["stage_changes"], [])
        attributed_before = {**case, "failure_attribution": {"trial": 1, "kind": "stage", "stage": "preprocess", "failed_checks": []}}
        attributed_after = {**after_case, "failure_attribution": None}
        comparison = compare_records({**before, "cases": [attributed_before]}, {**after, "cases": [attributed_after]})
        self.assertEqual(comparison["cases"][0]["attribution_change"]["before"]["stage"], "preprocess")
        self.assertIsNone(comparison["cases"][0]["attribution_change"]["after"])

    def test_failure_attribution_distinguishes_stage_grader_and_runner(self) -> None:
        failed = {"passed": False, "checks": {"result": False, "safety": True}, "dimensions": {"completion": False, "safety": True, "process": True}}
        stages = [
            {"stage": "prepare_single_database", "success": False, "failure_type": "private-error"},
            {"stage": "mcp_tool_call", "success": False, "failure_type": "tool_error"},
            {"stage": "execute_single_database", "success": False, "failure_type": "result_failed"},
        ]
        self.assertEqual(attribute_failure({"grade": failed, "stages": stages}), {"kind": "stage", "stage": "mcp_tool_call", "failed_checks": []})
        self.assertEqual(attribute_failure({"grade": failed, "stages": [{"stage": "retrieve_schema", "success": False}]}), {"kind": "stage", "stage": "retrieve_schema", "failed_checks": []})
        self.assertEqual(attribute_failure({"grade": failed, "stages": [], "error": None}), {"kind": "grader", "stage": None, "failed_checks": ["result"]})
        self.assertEqual(attribute_failure({"grade": failed, "stages": [], "error": "private-error"}), {"kind": "runner", "stage": None, "failed_checks": []})
        self.assertIsNone(attribute_failure({"grade": {**failed, "passed": True}, "stages": stages}))

    def test_failure_summary_and_export_keep_attribution_without_error_text(self) -> None:
        failed = {"passed": False, "checks": {"result": False}, "dimensions": {"completion": False, "safety": True, "process": True}}
        attempts = [
            {"trial": 1, "grade": failed, "error": "private-error", "stages": [{"stage": "mcp_tool_call", "success": False, "elapsed_ms": 3, "failure_type": "private-error"}]},
            {"trial": 2, "grade": failed, "error": None, "stages": []},
        ]
        case = {"task_id": "a", "category": "mock", "kind": "model", "incomplete": False, "pass3": False, "grade": failed, "attempts": attempts, "stage_metrics": None}
        record = {"id": "test00000001", "mode": "live", "status": "completed", "task_count": 1, "trials": 3, "cases": [case]}
        EvaluationService._summarize(record)
        self.assertEqual(case["failure_attribution"], {"trial": 1, "kind": "stage", "stage": "mcp_tool_call", "failed_checks": []})
        self.assertEqual(record["summary"]["failure_summary"], [
            {"kind": "grader", "stage": None, "count": 1},
            {"kind": "stage", "stage": "mcp_tool_call", "count": 1},
        ])
        self.assertEqual(failure_summary([case])[1]["count"], 1)
        exported = export_report(record)
        self.assertEqual(exported["cases"][0]["failure_attribution"]["stage"], "mcp_tool_call")
        self.assertEqual(exported["summary"]["failure_summary"][0]["kind"], "grader")
        self.assertNotIn("private-error", json.dumps(exported))

        old_attempt = {"trial": 1, "grade": failed, "error": None}
        old_record = {"task_count": 1, "trials": 1, "cases": [{"task_id": "old", "grade": failed, "attempts": [old_attempt]}]}
        EvaluationService._summarize(old_record)
        self.assertNotIn("failure_attribution", old_attempt)
        self.assertNotIn("failure_attribution", old_record["cases"][0])
        self.assertIsNone(old_record["summary"]["failure_summary"])

    def test_background_api_progress_and_completion(self) -> None:
        with TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            started = client.post("/api/eval/runs/start", headers=headers, json={"mode": "demo", "task_ids": ["metric.paid_vs_list", "time.single_day"]})
            self.assertEqual(started.status_code, 200, started.text)
            run_id = started.json()["id"]
            for _ in range(40):
                detail = client.get(f"/api/eval/runs/{run_id}", headers=headers).json()
                if detail["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(detail["status"], "completed", detail)
            self.assertEqual(detail["progress"]["completed_attempts"], 2)
            self.assertEqual(detail["summary"]["passed"], 2)

    def test_background_rejects_concurrent_run(self) -> None:
        service = EvaluationService()
        entered, release = Event(), Event()
        original = service._execute_case

        def held_case(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)

        with patch.object(service, "_execute_case", side_effect=held_case):
            started = service.start("demo", ["metric.paid_vs_list"])
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(RuntimeError):
                    service.start("demo", ["time.single_day"])
            finally:
                release.set()
            for _ in range(40):
                if service.get_run(started["id"])["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(service.get_run(started["id"])["status"], "completed")

    def test_cancel_running_trials_keeps_partial_trace_and_releases_slot(self) -> None:
        service = EvaluationService()
        entered, release = Event(), Event()
        original = service._execute_case
        calls = 0

        def held_case(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)

        with patch.object(service, "_execute_case", side_effect=held_case):
            started = service.start("demo", ["metric.paid_vs_list", "time.single_day"], trials=3)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(service.cancel(started["id"])["status"], "cancel_requested")
                self.assertEqual(service.get_run(started["id"])["status"], "cancel_requested")
            finally:
                release.set()
            for _ in range(60):
                if service.get_run(started["id"])["status"] == "cancelled" and service._active_run_id is None:
                    break
                time.sleep(0.05)
        record = service.get_run(started["id"])
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(calls, 1)
        self.assertEqual(record["progress"]["completed_attempts"], 1)
        self.assertEqual(len(record["cases"][0]["attempts"]), 1)
        self.assertTrue(record["cases"][0]["incomplete"])
        self.assertEqual(record["summary"]["passed"], 0)
        self.assertEqual(record["summary"]["metrics"]["model_calls"], 0)
        with self.assertRaisesRegex(ValueError, "不能重放"):
            service.get_run(started["id"], replay=True)
        with self.assertRaisesRegex(ValueError, "已完成"):
            service.compare(started["id"], service.run("demo", ["metric.paid_vs_list"])["id"])
        with self.assertRaisesRegex(ValueError, "不能取消"):
            service.cancel(started["id"])
        restarted = service.start("demo", ["time.single_day"])
        for _ in range(60):
            if service.get_run(restarted["id"])["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(service.get_run(restarted["id"])["status"], "completed")

    def test_cancel_api_returns_conflict_for_finished_run(self) -> None:
        with TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            run = client.post("/api/eval/runs", headers=headers, json={"mode": "demo", "task_ids": ["mock.region_sales"]}).json()
            self.assertEqual(client.post(f"/api/eval/runs/{run['id']}/cancel", headers=headers).status_code, 409)
            self.assertEqual(client.post("/api/eval/runs/aaaaaaaaaaaa/cancel", headers=headers).status_code, 404)

    def test_cancel_queued_run_executes_no_trials(self) -> None:
        service = EvaluationService()
        occupied, release = Event(), Event()

        def hold_worker() -> None:
            occupied.set()
            release.wait(3)

        service._executor.submit(hold_worker)
        self.assertTrue(occupied.wait(3))
        started = service.start("demo", ["mock.region_sales", "time.single_day"])
        try:
            self.assertEqual(service.cancel(started["id"])["status"], "cancel_requested")
        finally:
            release.set()
        for _ in range(60):
            if service.get_run(started["id"])["status"] == "cancelled":
                break
            time.sleep(0.05)
        record = service.get_run(started["id"])
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["progress"]["completed_attempts"], 0)
        self.assertEqual(record["cases"], [])

    def test_cancel_active_run_through_admin_api(self) -> None:
        service = EvaluationService()
        entered, release = Event(), Event()
        original = service._execute_case

        def held_case(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)

        with patch("app.api.routes.evaluation_service", service), patch.object(service, "_execute_case", side_effect=held_case), TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            started = client.post("/api/eval/runs/start", headers=headers, json={"mode": "demo", "task_ids": ["mock.region_sales", "time.single_day"], "trials": 3})
            self.assertEqual(started.status_code, 200, started.text)
            run_id = started.json()["id"]
            try:
                self.assertTrue(entered.wait(3))
                response = client.post(f"/api/eval/runs/{run_id}/cancel", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "cancel_requested")
                self.assertEqual(client.get(f"/api/eval/runs/{run_id}", headers=headers).json()["status"], "cancel_requested")
                self.assertEqual(next(run for run in client.get("/api/eval/runs", headers=headers).json() if run["id"] == run_id)["status"], "cancel_requested")
            finally:
                release.set()
            for _ in range(60):
                detail = client.get(f"/api/eval/runs/{run_id}", headers=headers).json()
                if detail["status"] == "cancelled":
                    break
                time.sleep(0.05)
            self.assertEqual(detail["status"], "cancelled")
            self.assertEqual(detail["progress"]["completed_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
