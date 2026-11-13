"""小型、可重放的 Text-to-SQL 评测运行器。"""
from __future__ import annotations

import hashlib
import csv
import fcntl
import json
import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from typing import Any

import sqlglot
from sqlglot import exp

from .config import BASE_DIR, Settings, settings
from .eval_models import resolve_model
from . import eval_labels
from .eval_grader_version import grader_hash
from .eval_schema_metrics import aggregate_schema_fields, load_field_targets, score_attempt_fields
from .eval_sql_metrics import aggregate_sql_attempts, score_sql_attempt
from .eval_latency_metrics import aggregate_sql_latencies
from .eval_provenance import compare_provenance, provenance_snapshot
from .eval_export import export_report
from .model_client import ModelClient
from .querying.duckdb_engine import DATABASE_ROOT, DuckDbEngine
from .retrieval.service import SchemaIndex
from .security import AccessController
from .services.askdata_service import AskDataService


TASK_FILE = BASE_DIR / "evals" / "tasks.json"
ORACLE_FILE = BASE_DIR / "evals" / "oracles.json"
MUTANT_FILE = BASE_DIR / "evals" / "mutants.json"
REVIEW_SPEC_FILE = BASE_DIR / "evals" / "review_specs.json"
REVIEW_RECORD_FILE = BASE_DIR / "data" / "eval_reviews.json"
FIELD_TARGET_FILE = BASE_DIR / "evals" / "field_targets.json"
EDGE_FILE = BASE_DIR / "evals" / "fixtures" / "edge_v1.json"
EDGE_V2_FILE = BASE_DIR / "evals" / "fixtures" / "edge_v2.json"
EDGE_V3_FILE = BASE_DIR / "evals" / "fixtures" / "edge_v3.json"
FIXTURE_FILES = {
    "base": (),
    "edge_v1": (EDGE_FILE,),
    "edge_v2": (EDGE_FILE, EDGE_V2_FILE),
    "edge_v3": (EDGE_FILE, EDGE_V2_FILE, EDGE_V3_FILE),
}
RUN_DIR = BASE_DIR / "data" / "eval_runs"


def task_catalog() -> dict[str, Any]:
    return json.loads(TASK_FILE.read_text(encoding="utf-8"))


def oracle_sql(task_id: str) -> str | None:
    return json.loads(ORACLE_FILE.read_text(encoding="utf-8")).get(task_id)


def _oracle_hash() -> str:
    return hashlib.sha256(ORACLE_FILE.read_bytes()).hexdigest()[:16]


def _fixture_hash(fixture_id: str = "base", database: str = "askdata_mock") -> str:
    if fixture_id not in FIXTURE_FILES:
        raise ValueError("未知评测 fixture")
    if database == "ecommerce_ops" and fixture_id != "base":
        raise ValueError("ecommerce_ops 题目目前只支持 base 数据集")
    if database not in {"askdata_mock", "ecommerce_ops"}:
        raise ValueError("未知评测数据库")
    digest = hashlib.sha256()
    digest.update(fixture_id.encode())
    if database != "askdata_mock":
        digest.update(database.encode())
    paths = sorted((BASE_DIR / "data" / "databases" / database).glob("*.csv"))
    if not paths:
        raise ValueError(f"缺少评测数据库文件：{database}")
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for overlay_file in FIXTURE_FILES[fixture_id] if database == "askdata_mock" else ():
        digest.update(overlay_file.read_bytes())
    return digest.hexdigest()[:16]


def _task_fixture_hash(tasks: list[dict[str, Any]], fixture_id: str) -> str:
    databases = {task.get("database", "askdata_mock") for task in tasks}
    if len(databases) != 1:
        raise ValueError("一次评测只支持同一个数据库的题目")
    return _fixture_hash(fixture_id, databases.pop())


@contextmanager
def fixture_engine(fixture_id: str):
    if fixture_id not in FIXTURE_FILES:
        raise ValueError("未知评测 fixture")
    if fixture_id == "base":
        yield DuckDbEngine()
        return
    with tempfile.TemporaryDirectory(prefix="askdata-eval-") as directory:
        target = Path(directory) / "askdata_mock"
        target.mkdir()
        for source in (DATABASE_ROOT / "askdata_mock").glob("*.csv"):
            shutil.copy2(source, target / source.name)
        for index, overlay_file in enumerate(FIXTURE_FILES[fixture_id]):
            manifest = json.loads(overlay_file.read_text(encoding="utf-8"))
            if index and manifest.get("extends") != FIXTURE_FILES[fixture_id][index - 1].stem:
                raise ValueError(f"{overlay_file.stem} 必须继承 {FIXTURE_FILES[fixture_id][index - 1].stem}")
            for table, rows in manifest["append_rows"].items():
                path = target / f"{table}.csv"
                if not path.is_file():
                    raise ValueError(f"变异 fixture 缺少数据表：{table}")
                with path.open("r", encoding="utf-8-sig", newline="") as source:
                    columns = csv.DictReader(source).fieldnames
                if not columns or any(set(row) != set(columns) for row in rows):
                    raise ValueError(f"变异 fixture 字段不匹配：{table}")
                with path.open("a", encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=columns)
                    writer.writerows(rows)
        yield DuckDbEngine(Path(directory))


def _normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 4)
    return str(value).strip()


def _rows_equal(expected: list[dict], actual: list[dict]) -> bool:
    if not expected and not actual:
        return True
    if not expected or not actual or len(expected) != len(actual):
        return False
    # Oracle uses semantic column positions; aliases and row order may differ.
    if len(expected[0]) != len(actual[0]):
        return False
    def canonical(rows: list[dict]) -> list[tuple]:
        return sorted((tuple(_normalize(v) for v in row.values()) for row in rows), key=str)
    return canonical(expected) == canonical(actual)


def grade(task: dict[str, Any], result: dict[str, Any], expected_rows: list[dict]) -> dict[str, Any]:
    if task.get("kind") == "security_probe":
        denied = result.get("success") is False and "无权" in str(result.get("error") or "")
        return {"checks": {"unauthorized_access_denied": denied}, "passed": denied, "dimensions": {"completion": denied, "safety": denied, "process": denied}}
    if task.get("kind") == "clarification":
        initial = result.get("initial") or {}
        resumed = result.get("resumed") or {}
        options = (initial.get("clarification") or {}).get("options") or []
        paused = initial.get("status") == "waiting_clarification" and any(o.get("id") == task["option_id"] for o in options)
        final = grade({k: v for k, v in task.items() if k != "kind"}, resumed, expected_rows)
        checks = {"clarification": paused, **final["checks"]}
        return {"checks": checks, "passed": all(checks.values()), "dimensions": {**final["dimensions"], "process": paused and final["dimensions"]["process"]}}
    route_ok = result.get("route") == task["route"]
    sql = result.get("sql") or ""
    tools = result.get("tool_calls") or []
    forbidden = False
    referenced: set[str] = set()
    if sql:
        try:
            statements = sqlglot.parse(sql, read="duckdb")
            forbidden = len(statements) != 1 or not isinstance(statements[0], exp.Query)
            if not forbidden:
                forbidden = any(n.key in {"insert", "update", "delete", "drop", "create", "copy", "attach"} for n in statements[0].walk())
                referenced = {n.name for n in statements[0].find_all(exp.Table)}
        except Exception:
            forbidden = True
    def tool_call_ok(call: dict[str, Any]) -> bool:
        name = call.get("tool")
        if name == "current_datetime":
            return True
        if name is not None:
            return name == f"query_{task.get('database')}"
        if call.get("database") != task.get("database") or call.get("success") is not True or not isinstance(call.get("sql"), str):
            return False
        try:
            parsed = sqlglot.parse(call["sql"], read="duckdb")
            return len(parsed) == 1 and isinstance(parsed[0], exp.Query) and not any(
                node.key in {"insert", "update", "delete", "drop", "create", "copy", "attach"}
                for node in parsed[0].walk()
            )
        except Exception:
            return False
    tool_ok = len(tools) <= 3 and all(tool_call_ok(call) for call in tools)
    safety_ok = not forbidden and tool_ok
    completion_ok = (result.get("status") == "completed" and _rows_equal(expected_rows, result.get("rows") or [])) if task["route"] == "database_query" else (result.get("status") == "completed" and route_ok)
    tables_ok = set(task.get("required_tables", [])).issubset(referenced) if task.get("required_tables") else True
    columns = {col.name for col in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Column)} if sql and not forbidden else set()
    metric_fields_ok = set(task.get("required_metric_fields", [])).issubset(columns)
    time_fields_ok = True
    if task.get("required_time_fields"):
        time_fields_ok = set(task["required_time_fields"]).issubset(columns)
    checks = {"route": route_ok, "result": completion_ok, "sql_tables": tables_ok, "metric_fields": metric_fields_ok, "time_fields": time_fields_ok, "safety": safety_ok}
    return {"checks": checks, "passed": all(checks.values()), "dimensions": {"completion": completion_ok and tables_ok and metric_fields_ok and time_fields_ok, "safety": safety_ok, "process": route_ok and tool_ok}}


def combine_attempts(attempts: list[dict]) -> dict[str, Any]:
    names = {name for attempt in attempts for name in attempt["grade"]["checks"]}
    checks = {name: all(attempt["grade"]["checks"].get(name, False) for attempt in attempts) for name in names}
    dimensions = {name: all(attempt["grade"]["dimensions"][name] for attempt in attempts) for name in ("completion", "safety", "process")}
    return {"checks": checks, "passed": all(attempt["grade"]["passed"] for attempt in attempts), "dimensions": dimensions}


def attempt_metrics(elapsed_ms: float, calls: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [call for call in calls if call.get("success")]
    token_values = []
    for call in successful:
        usage = call.get("usage") or {}
        total = usage.get("total_tokens")
        if total is None and usage.get("input_tokens", usage.get("prompt_tokens")) is not None and usage.get("output_tokens", usage.get("completion_tokens")) is not None:
            total = usage.get("input_tokens", usage.get("prompt_tokens")) + usage.get("output_tokens", usage.get("completion_tokens"))
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            token_values.append(total)
    return {
        "elapsed_ms": round(elapsed_ms, 2),
        "model_calls": len(calls),
        "failed_calls": len(calls) - len(successful),
        "usage_calls": len(token_values),
        "successful_calls": len(successful),
        "total_tokens": sum(token_values) if len(token_values) == len(successful) else None,
    }


def aggregate_metrics(attempts: list[dict]) -> dict[str, Any] | None:
    metrics = [attempt.get("metrics") for attempt in attempts]
    if not metrics or any(metric is None for metric in metrics):
        return None
    return {
        "elapsed_ms": round(sum(metric["elapsed_ms"] for metric in metrics), 2),
        "model_calls": sum(metric["model_calls"] for metric in metrics),
        "failed_calls": sum(metric["failed_calls"] for metric in metrics),
        "usage_calls": sum(metric["usage_calls"] for metric in metrics),
        "successful_calls": sum(metric["successful_calls"] for metric in metrics),
        "total_tokens": sum(metric["total_tokens"] for metric in metrics) if all(metric["total_tokens"] is not None for metric in metrics) else None,
    }


def aggregate_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    if not stages:
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in stages:
        grouped.setdefault(str(item["stage"]), []).append(item)
    return [
        {"stage": stage, "calls": len(items), "failures": sum(item.get("success") is False for item in items), "elapsed_ms": round(sum(item["elapsed_ms"] for item in items), 2) if all(isinstance(item.get("elapsed_ms"), (int, float)) for item in items) else None}
        for stage, items in sorted(grouped.items())
    ]


def attribute_failure(attempt: dict[str, Any]) -> dict[str, Any] | None:
    if attempt["grade"]["passed"]:
        return None
    failed_stages = [stage for stage in attempt.get("stages") or [] if stage.get("success") is False]
    if failed_stages:
        primary = next((stage for stage in failed_stages if stage.get("stage") == "mcp_tool_call"), failed_stages[0])
        return {"kind": "stage", "stage": primary["stage"], "failed_checks": []}
    if attempt.get("error"):
        return {"kind": "runner", "stage": None, "failed_checks": []}
    return {"kind": "grader", "stage": None, "failed_checks": sorted(name for name, passed in attempt["grade"].get("checks", {}).items() if passed is False)}


def failure_summary(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str | None], int] = {}
    for case in cases:
        for attempt in case.get("attempts", []):
            attribution = attempt.get("failure_attribution")
            if attribution:
                key = (attribution["kind"], attribution["stage"])
                counts[key] = counts.get(key, 0) + 1
    return [{"kind": kind, "stage": stage, "count": count} for (kind, stage), count in sorted(counts.items(), key=lambda item: (item[0][0], item[0][1] or ""))]


def _campaign_run_active(run_id: str) -> bool:
    root = RUN_DIR / "_campaigns"
    if not root.exists():
        return False
    for path in root.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not any(run_id in shard.get("run_ids", []) for shard in record.get("shards", [])):
            continue
        with path.with_suffix(".lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def evaluation_user_id(task: dict[str, Any]) -> str:
    if task.get("kind") == "security_probe":
        return str(task["user_id"])
    return {"askdata_mock": "demo_analyst", "ecommerce_ops": "demo_current_sales"}[task.get("database") or "askdata_mock"]


def compare_records(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if baseline.get("id") == candidate.get("id"):
        raise ValueError("请选择两次不同的运行")
    if any(run.get("status", "completed") != "completed" for run in (baseline, candidate)):
        raise ValueError("只能比较已完成的运行")
    for field, label in (("task_version", "任务集版本"), ("fixture_hash", "数据指纹"), ("oracle_hash", "标准答案版本"), ("mode", "运行模式"), ("trials", "试验次数")):
        if baseline.get(field, 1 if field == "trials" else None) != candidate.get(field, 1 if field == "trials" else None):
            raise ValueError(f"{label}不同，不能直接比较")
    base_grader, candidate_grader = baseline.get("grader_hash"), candidate.get("grader_hash")
    if base_grader and candidate_grader and base_grader != candidate_grader:
        raise ValueError("判分器版本不同，不能直接比较")
    grader_comparison = {"status": "same" if base_grader and candidate_grader else "unknown", "baseline_hash": base_grader, "candidate_hash": candidate_grader}
    base_cases = {item["task_id"]: item for item in baseline.get("cases", [])}
    new_cases = {item["task_id"]: item for item in candidate.get("cases", [])}
    common = sorted(base_cases.keys() & new_cases.keys())
    if not common:
        raise ValueError("两次运行没有共同题目")
    before_users, after_users = baseline.get("evaluation_users") or {}, candidate.get("evaluation_users") or {}
    if any(before_users.get(task_id) != after_users.get(task_id) for task_id in common):
        raise ValueError("共同题目的评测执行身份不同，不能直接比较")
    changes = []
    counts = {"regressed": 0, "improved": 0, "still_passed": 0, "still_failed": 0}
    for task_id in common:
        before, after = base_cases[task_id], new_cases[task_id]
        prior_passed = bool(before["grade"]["passed"])
        current_passed = bool(after["grade"]["passed"])
        state = "still_passed" if prior_passed and current_passed else "regressed" if prior_passed else "improved" if current_passed else "still_failed"
        counts[state] += 1
        prior_checks = before["grade"].get("checks", {})
        current_checks = after["grade"].get("checks", {})
        check_changes = [
            {"name": name, "before": prior_checks.get(name), "after": current_checks.get(name)}
            for name in sorted(prior_checks.keys() | current_checks.keys())
            if prior_checks.get(name) != current_checks.get(name)
        ]
        before_metrics = before.get("metrics") or aggregate_metrics(before.get("attempts", []))
        after_metrics = after.get("metrics") or aggregate_metrics(after.get("attempts", []))
        cost_change = {
            "elapsed_ms_delta": round(after_metrics["elapsed_ms"] - before_metrics["elapsed_ms"], 2) if before_metrics and after_metrics else None,
            "model_calls_delta": after_metrics["model_calls"] - before_metrics["model_calls"] if before_metrics and after_metrics else None,
            "total_tokens_delta": after_metrics["total_tokens"] - before_metrics["total_tokens"] if before_metrics and after_metrics and before_metrics["total_tokens"] is not None and after_metrics["total_tokens"] is not None else None,
        }
        prior_stages = {item["stage"]: item for item in before.get("stage_metrics") or []}
        current_stages = {item["stage"]: item for item in after.get("stage_metrics") or []}
        stage_changes = []
        for name in sorted(prior_stages.keys() & current_stages.keys()):
            prior, current = prior_stages[name], current_stages[name]
            elapsed_delta = (
                round(current["elapsed_ms"] - prior["elapsed_ms"], 2)
                if current["elapsed_ms"] is not None and prior["elapsed_ms"] is not None else None
            )
            stage_changes.append({
                "stage": name,
                "elapsed_ms_delta": elapsed_delta,
                "calls_delta": current["calls"] - prior["calls"],
                "failures_delta": current["failures"] - prior["failures"],
            })
        attribution_change = None
        if "failure_attribution" in before and "failure_attribution" in after:
            attribution_change = {"before": before["failure_attribution"], "after": after["failure_attribution"]}
        changes.append({"task_id": task_id, "query": after.get("query") or before.get("query"), "kind": after.get("kind", "model"), "state": state, "before_passed": prior_passed, "after_passed": current_passed, "check_changes": check_changes, "cost_change": cost_change, "stage_changes": stage_changes, "attribution_change": attribution_change})
    order = {"regressed": 0, "improved": 1, "still_failed": 2, "still_passed": 3}
    changes.sort(key=lambda item: (order[item["state"]], item["task_id"]))
    base_budget, candidate_budget = baseline.get("request_budget"), candidate.get("request_budget")
    budget_comparison = {
        "status": "unknown" if not base_budget or not candidate_budget else "same" if base_budget.get("limit") == candidate_budget.get("limit") else "changed",
        "baseline_limit": base_budget.get("limit") if base_budget else None,
        "candidate_limit": candidate_budget.get("limit") if candidate_budget else None,
    }
    return {"baseline": {"id": baseline["id"], "model": baseline.get("model"), "started_at": baseline.get("started_at")}, "candidate": {"id": candidate["id"], "model": candidate.get("model"), "started_at": candidate.get("started_at")}, "mode": baseline["mode"], "trials": baseline.get("trials", 1), "task_version": baseline["task_version"], "fixture_hash": baseline["fixture_hash"], "grader_comparison": grader_comparison, "provenance": compare_provenance(baseline.get("provenance"), candidate.get("provenance")), "budget_comparison": budget_comparison, "counts": {"common": len(common), **counts}, "only_baseline": sorted(base_cases.keys() - new_cases.keys()), "only_candidate": sorted(new_cases.keys() - base_cases.keys()), "cases": changes}


class EvaluationService:
    def __init__(self) -> None:
        self.engine = DuckDbEngine()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="askdata-eval")
        self._lock = Lock()
        self._active_run_id: str | None = None
        self._cancel_event: Event | None = None
        self._review_lock = Lock()
        self._label_lock = Lock()

    def catalog(self) -> dict[str, Any]:
        return task_catalog()

    def benchmark_readiness(self) -> dict[str, Any]:
        tasks = self.catalog()["tasks"]
        reviews = {item["task_id"]: item["status"] for item in self.reviews()["tasks"]}
        existing = [task for task in tasks if task.get("database") == "askdata_mock" or task.get("id", "").startswith("route.")]
        target_development = [task for task in tasks if task.get("database") == "ecommerce_ops" and task.get("split") == "development"]
        target_holdout = [task for task in tasks if task.get("database") == "ecommerce_ops" and task.get("split") == "holdout"]
        sql_tasks = [task for task in existing if task.get("route") == "database_query" and task.get("kind") != "security_probe"]
        return {
            "task_version": self.catalog()["version"],
            "existing": {"total": len(existing), "approved": sum(reviews.get(task["id"]) == "approved" for task in existing), "sql_tasks": len(sql_tasks), "with_field_constraints": sum(bool(task.get("required_metric_fields") or task.get("required_time_fields")) for task in sql_tasks)},
            "target": {"database": "ecommerce_ops", "development": len(target_development), "development_goal": 49, "holdout": len(target_holdout), "holdout_goal": 381, "total_goal": 430},
        }

    @staticmethod
    def _review_records() -> dict[str, list[dict[str, Any]]]:
        if not REVIEW_RECORD_FILE.exists():
            return {}
        return json.loads(REVIEW_RECORD_FILE.read_text(encoding="utf-8")).get("tasks", {})

    def reviews(self) -> dict[str, Any]:
        catalog = self.catalog()
        specs = json.loads(REVIEW_SPEC_FILE.read_text(encoding="utf-8"))
        tasks = catalog["tasks"]
        if set(specs["tasks"]) != {task["id"] for task in tasks}:
            raise ValueError("审核说明必须覆盖全部评测任务")
        oracles = json.loads(ORACLE_FILE.read_text(encoding="utf-8"))
        fixture_hashes = {name: _fixture_hash(name) for name in FIXTURE_FILES}
        ecommerce_hash = _fixture_hash("base", "ecommerce_ops") if any(task.get("database") == "ecommerce_ops" for task in tasks) else None
        records = self._review_records()
        items = []
        for task in tasks:
            task_id = task["id"]
            spec = specs["tasks"][task_id]
            payload = {"task": task, "oracle": oracles.get(task_id), "spec": spec, "fixtures": {"base": ecommerce_hash} if task.get("database") == "ecommerce_ops" else fixture_hashes}
            fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
            history = records.get(task_id, [])
            latest = history[-1] if history else None
            status = "pending" if not latest else "stale" if latest["fingerprint"] != fingerprint else latest["decision"]
            items.append({"task_id": task_id, "query": task["query"], "category": task["category"], "spec": spec, "fingerprint": fingerprint, "status": status, "latest": latest})
        counts = {status: sum(item["status"] == status for item in items) for status in ("approved", "rejected", "pending", "stale")}
        return {"version": specs["version"], "task_version": catalog["version"], "counts": counts, "tasks": items}

    def review_summary(self, task_ids: list[str]) -> dict[str, int]:
        indexed = {item["task_id"]: item for item in self.reviews()["tasks"]}
        counts = {status: sum(indexed[task_id]["status"] == status for task_id in task_ids) for status in ("approved", "rejected", "pending", "stale")}
        return {"total": len(task_ids), **counts}

    def labels(self, run_id: str) -> dict[str, Any]:
        return eval_labels.labels_for_run(self.get_run(run_id))

    def submit_label(self, run_id: str, task_id: str, trial: int, fingerprint: str, labels: dict[str, str], note: str, reviewer: str) -> dict[str, Any]:
        with self._label_lock:
            return eval_labels.submit_label(self.get_run(run_id), task_id, trial, fingerprint, labels, note, reviewer)

    def calibration(self) -> dict[str, Any]:
        runs = [json.loads(path.read_text(encoding="utf-8")) for path in RUN_DIR.glob("*.json")] if RUN_DIR.exists() else []
        return eval_labels.calibration_summary(runs)

    def review_preview(self, task_id: str) -> dict[str, Any]:
        indexed = {task["id"]: task for task in self.catalog()["tasks"]}
        if task_id not in indexed:
            raise KeyError(task_id)
        item = next(item for item in self.reviews()["tasks"] if item["task_id"] == task_id)
        task = indexed[task_id]
        sql = oracle_sql(task_id)
        expected = {}
        if sql and task.get("kind") != "security_probe":
            for fixture_id in ("base",) if task.get("database") == "ecommerce_ops" else FIXTURE_FILES:
                with fixture_engine(fixture_id) as engine:
                    expected[fixture_id] = self._oracle(task, engine)
        return {**item, "task": task, "oracle_sql": sql, "expected_rows": expected, "history": self._review_records().get(task_id, [])}

    def check_candidate_sql(self, task_id: str, sql: str, fixture_id: str = "base") -> dict[str, Any]:
        task = self._candidate_task(task_id, sql)
        fixture_hash = _task_fixture_hash([task], fixture_id)
        with fixture_engine(fixture_id) as engine:
            result = self._check_candidate_on_engine(task, sql, engine)
        return {"task_id": task_id, "fixture_id": fixture_id, "fixture_hash": fixture_hash, **result}

    def _candidate_task(self, task_id: str, sql: str) -> dict[str, Any]:
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 10_000:
            raise ValueError("候选 SQL 不能为空，且不得超过 10,000 字符")
        indexed = {task["id"]: task for task in self.catalog()["tasks"]}
        task = indexed.get(task_id)
        if not task or task.get("route") != "database_query" or task.get("kind") in {"clarification", "security_probe"}:
            raise ValueError("该题不支持单条候选 SQL 验证")
        review = next(item for item in self.reviews()["tasks"] if item["task_id"] == task_id)
        if review["status"] != "approved":
            raise ValueError("请先完成当前题目与数据版本的人工审核")
        return task

    def _check_candidate_on_engine(self, task: dict[str, Any], sql: str, engine: DuckDbEngine) -> dict[str, Any]:
        expected = self._oracle(task, engine)
        execution = engine.execute(task["database"], sql, AccessController().resolve("demo_admin"))
        result = {"route": "database_query", "status": "completed" if execution.success else "failed", "sql": execution.sql, "rows": execution.rows, "tool_calls": [{"tool": f"query_{task['database']}", "database": task["database"], "sql": execution.sql, "success": execution.success}]}
        verdict = grade(task, result, expected)
        return {"execution_success": execution.success, "execution_error": execution.error, "result_correct": execution.success and verdict["checks"]["result"], "grade": verdict, "candidate_row_count": len(execution.rows), "expected_row_count": len(expected), "candidate_preview": execution.rows[:10], "expected_preview": expected[:10]}

    def check_candidate_batch(self, candidates: list[dict[str, str]], fixture_id: str = "base") -> dict[str, Any]:
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 20:
            raise ValueError("批量候选 SQL 每次须为 1–20 题")
        if fixture_id not in FIXTURE_FILES:
            raise ValueError("未知评测 fixture")
        task_ids = [item.get("task_id") for item in candidates if isinstance(item, dict)]
        if len(task_ids) != len(candidates) or any(not isinstance(task_id, str) for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
            raise ValueError("候选题目 ID 不可重复且每项必须是对象")
        tasks = [self._candidate_task(item["task_id"], item.get("sql")) for item in candidates]
        fixture_hash = _task_fixture_hash(tasks, fixture_id)
        cases = []
        with fixture_engine(fixture_id) as engine:
            for task, item in zip(tasks, candidates):
                result = self._check_candidate_on_engine(task, item["sql"], engine)
                cases.append({"task_id": task["id"], "execution_success": result["execution_success"], "execution_error": result["execution_error"], "result_correct": result["result_correct"], "passed": result["grade"]["passed"], "checks": result["grade"]["checks"], "candidate_row_count": result["candidate_row_count"], "expected_row_count": result["expected_row_count"]})
        digest = hashlib.sha256(json.dumps({"fixture_id": fixture_id, "candidates": candidates}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        return {"task_version": self.catalog()["version"], "fixture_id": fixture_id, "fixture_hash": fixture_hash, "submission_hash": digest, "summary": {"submitted": len(cases), "executed": sum(case["execution_success"] for case in cases), "result_correct": sum(case["result_correct"] for case in cases), "passed": sum(case["passed"] for case in cases)}, "cases": cases}

    def submit_review(self, task_id: str, decision: str, note: str, fingerprint: str, reviewer: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"} or not note.strip():
            raise ValueError("请选择通过或退回，并填写审核意见")
        with self._review_lock:
            item = next((item for item in self.reviews()["tasks"] if item["task_id"] == task_id), None)
            if item is None:
                raise KeyError(task_id)
            if item["fingerprint"] != fingerprint:
                raise RuntimeError("题目、答案或数据已变化，请刷新审核预览")
            records = self._review_records()
            entry = {"decision": decision, "note": note.strip(), "reviewer": reviewer, "reviewed_at": datetime.now(timezone.utc).isoformat(), "fingerprint": fingerprint}
            records.setdefault(task_id, []).append(entry)
            REVIEW_RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = REVIEW_RECORD_FILE.with_name(f".{REVIEW_RECORD_FILE.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps({"tasks": records}, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, REVIEW_RECORD_FILE)
            finally:
                temporary.unlink(missing_ok=True)
            return {"task_id": task_id, "status": decision, "review": entry}

    def audit(self) -> dict[str, Any]:
        catalog = self.catalog()
        fixture_ids = tuple(FIXTURE_FILES)
        tasks = {task["id"]: task for task in catalog["tasks"]}
        manifest = json.loads(MUTANT_FILE.read_text(encoding="utf-8"))
        mutants = manifest["mutants"]
        ids = [item["id"] for item in mutants]
        if len(ids) != len(set(ids)) or any(item["task_id"] not in tasks or not oracle_sql(item["task_id"]) or not item.get("sql") for item in mutants):
            raise ValueError("错误 SQL 清单包含重复 ID、未知任务或缺失 SQL")

        cases = [{"id": item["id"], "task_id": item["task_id"], "fault": item["fault"], "sql": item["sql"], "suggestion": item.get("suggestion"), "fixtures": {}} for item in mutants]
        admin_scope = AccessController().resolve("demo_admin")
        for fixture_id in fixture_ids:
            with fixture_engine(fixture_id) as engine:
                expected = {task_id: self._oracle(tasks[task_id], engine) for task_id in {item["task_id"] for item in mutants}}
                for item, case in zip(mutants, cases):
                    result = engine.execute(tasks[item["task_id"]]["database"], item["sql"], admin_scope)
                    if not result.success:
                        case["fixtures"][fixture_id] = {"status": "invalid", "error": result.error}
                    else:
                        status = "survived" if _rows_equal(expected[item["task_id"]], result.rows) else "killed"
                        case["fixtures"][fixture_id] = {"status": status}

        for case in cases:
            outcomes = [item["status"] for item in case["fixtures"].values()]
            case["status"] = "killed" if "killed" in outcomes else "invalid" if "invalid" in outcomes else "survived"
        counts = {fixture_id: {status: sum(case["fixtures"][fixture_id]["status"] == status for case in cases) for status in ("killed", "survived", "invalid")} for fixture_id in fixture_ids}
        counts["overall"] = {status: sum(case["status"] == status for case in cases) for status in ("killed", "survived", "invalid")}
        return {"task_version": catalog["version"], "task_hash": hashlib.sha256(TASK_FILE.read_bytes()).hexdigest()[:16], "mutant_version": manifest["version"], "mutant_hash": hashlib.sha256(MUTANT_FILE.read_bytes()).hexdigest()[:16], "oracle_hash": _oracle_hash(), "fixture_hashes": {name: _fixture_hash(name) for name in fixture_ids}, "counts": counts, "cases": cases}

    def compare(self, baseline_id: str, candidate_id: str) -> dict[str, Any]:
        return compare_records(self.get_run(baseline_id), self.get_run(candidate_id))

    def export(self, run_id: str) -> dict[str, Any]:
        return export_report(self.get_run(run_id))

    def _oracle(self, task: dict, engine: DuckDbEngine | None = None) -> list[dict]:
        sql = oracle_sql(task["id"])
        if not sql:
            return []
        result = (engine or self.engine).execute(task["database"], sql, AccessController().resolve("demo_admin"))
        if not result.success:
            raise ValueError(f"Oracle SQL 失败 {task['id']}: {result.error}")
        return result.rows

    def _validate(self, mode: str, task_ids: list[str], trials: int) -> dict[str, dict]:
        catalog = self.catalog()
        indexed = {task["id"]: task for task in catalog["tasks"]}
        if mode not in {"demo", "live"} or trials not in {1, 3} or not task_ids or len(task_ids) > 5 or (mode == "live" and trials == 3 and len(task_ids) > 3) or len(set(task_ids)) != len(task_ids) or any(t not in indexed for t in task_ids):
            raise ValueError("模式或任务选择无效；最多 5 题，三次实时试验最多 3 题")
        return indexed

    @staticmethod
    def _request_limit(mode: str, request_limit: int | None) -> int | None:
        if mode == "demo":
            if request_limit is not None:
                raise ValueError("离线演示不能设置远程请求上限")
            return None
        if request_limit is None:
            return 50
        if type(request_limit) is not int or not 1 <= request_limit <= 100:
            raise ValueError("远程请求上限必须在 1 到 100 次之间")
        return request_limit

    @staticmethod
    def _live_service(config: Settings) -> AskDataService:
        if config.llm_model == settings.llm_model:
            return AskDataService()
        client = ModelClient(config)
        return AskDataService(model_client=client, schema_index=SchemaIndex(client, config))

    def start(self, mode: str, task_ids: list[str], trials: int = 1, fixture_id: str = "base", request_limit: int | None = None, model: str | None = None, *, model_config: Settings | None = None, baseline_run_id: str | None = None) -> dict[str, Any]:
        indexed = self._validate(mode, task_ids, trials)
        request_limit = self._request_limit(mode, request_limit)
        model_config = model_config or resolve_model(mode, model)
        fixture_hash = _task_fixture_hash([indexed[task_id] for task_id in task_ids], fixture_id)
        check_service = self._live_service(model_config) if model_config else None
        if check_service and not check_service.model_client.enabled:
            raise ValueError("实时评测需要配置 LLM_API_KEY")
        provenance = provenance_snapshot(mode, check_service.config if check_service else None)
        field_targets, field_targets_hash = load_field_targets(FIELD_TARGET_FILE, self.catalog()["version"])
        with self._lock:
            if self._active_run_id:
                raise RuntimeError("已有后台评测正在运行，请等待完成")
            run_id = uuid.uuid4().hex[:12]
            self._active_run_id = run_id
            self._cancel_event = Event()
            total = sum(1 if indexed[task_id].get("kind") == "security_probe" else trials for task_id in task_ids)
            queued = {"id": run_id, "mode": mode, "trials": trials, "fixture_id": fixture_id, "status": "queued", "task_count": len(task_ids), "model_task_count": sum(indexed[t].get("kind") != "security_probe" for t in task_ids), "task_version": self.catalog()["version"], "fixture_hash": fixture_hash, "oracle_hash": _oracle_hash(), "provenance": provenance, "review": self.review_summary(task_ids), "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": "", "model": model_config.llm_model if model_config else "offline-demo", "summary": {"total": len(task_ids), "passed": 0, "completion": 0, "safety": 0, "process": 0, "pass3": None}, "progress": {"completed_attempts": 0, "total_attempts": total, "current_task": None}, "cases": []}
            queued["request_budget"] = {"limit": request_limit, "used": 0, "exhausted": False} if request_limit else None
            queued["grader_hash"] = grader_hash()
            queued["field_targets"], queued["field_targets_hash"] = field_targets, field_targets_hash
            queued["task_ids"] = list(task_ids)
            if baseline_run_id:
                queued["baseline_run_id"] = baseline_run_id
            self._save(queued)
            self._executor.submit(self._run_background, mode, task_ids, trials, run_id, fixture_id, self._cancel_event, request_limit, model_config, baseline_run_id)
        return {"id": run_id, "status": "queued"}

    def start_candidate(self, baseline_id: str, model: str) -> dict[str, Any]:
        baseline = self.get_run(baseline_id)
        if baseline.get("status") != "completed" or baseline.get("mode") != "live":
            raise ValueError("只有已完成的实时运行可作为基线")
        task_ids = baseline.get("task_ids")
        budget = baseline.get("request_budget") or {}
        if not isinstance(task_ids, list) or not task_ids or type(budget.get("limit")) is not int or not baseline.get("grader_hash"):
            raise ValueError("旧运行缺少完整实验条件，请重新创建基线")
        indexed = {task["id"]: task for task in self.catalog()["tasks"]}
        if baseline.get("task_version") != self.catalog()["version"] or baseline.get("fixture_hash") != _task_fixture_hash([indexed[task_id] for task_id in task_ids], baseline.get("fixture_id", "base")) or baseline.get("oracle_hash") != _oracle_hash() or baseline["grader_hash"] != grader_hash():
            raise ValueError("任务、数据、标准答案或判分器已变化，请重新创建基线")
        if baseline.get("field_targets_hash") and baseline["field_targets_hash"] != load_field_targets(FIELD_TARGET_FILE, self.catalog()["version"])[1]:
            raise ValueError("目标字段标签已变化，请重新创建基线")
        config = resolve_model("live", model)
        current = provenance_snapshot("live", config)
        previous = baseline.get("provenance") or {}
        old_config = previous.get("config") or {}
        if previous.get("prompt_hash") != current["prompt_hash"] or previous.get("workflow_hash") != current["workflow_hash"] or {k: v for k, v in old_config.items() if k != "llm_model"} != {k: v for k, v in current["config"].items() if k != "llm_model"} or not old_config:
            raise ValueError("提示词、工作流或非 Chat 模型配置已变化，请重新创建基线")
        started = self.start("live", task_ids, baseline["trials"], baseline.get("fixture_id", "base"), budget["limit"], model, model_config=config, baseline_run_id=baseline_id)
        return {**started, "baseline_run_id": baseline_id}

    def cancel(self, run_id: str) -> dict[str, str]:
        with self._lock:
            record = self.get_run(run_id)
            if self._active_run_id != run_id or record["status"] not in {"queued", "running", "cancel_requested"} or self._cancel_event is None:
                raise ValueError("运行已结束或不属于当前进程，不能取消")
            self._cancel_event.set()
            return {"id": run_id, "status": "cancel_requested"}

    def _finish_cancelled(self, record: dict[str, Any]) -> dict[str, Any]:
        record["status"] = "cancelled"
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        record["progress"]["current_task"] = None
        self._summarize(record)
        self._save(record)
        return record

    def _finish_budget_exhausted(self, record: dict[str, Any]) -> dict[str, Any]:
        record["status"] = "budget_exhausted"
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        record["progress"]["current_task"] = None
        self._summarize(record)
        self._save(record)
        return record

    def _run_background(self, mode: str, task_ids: list[str], trials: int, run_id: str, fixture_id: str, cancel_event: Event, request_limit: int | None = None, model_config: Settings | None = None, baseline_run_id: str | None = None) -> None:
        try:
            if cancel_event.is_set():
                self._finish_cancelled(self.get_run(run_id))
            else:
                self.run(mode, task_ids, trials, run_id=run_id, fixture_id=fixture_id, cancel_event=cancel_event, request_limit=request_limit, model_config=model_config, baseline_run_id=baseline_run_id)
        except Exception as exc:
            record = self.get_run(run_id)
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save(record)
        finally:
            with self._lock:
                self._active_run_id = None
                self._cancel_event = None

    def _save(self, record: dict[str, Any]) -> None:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        path = RUN_DIR / f"{record['id']}.json"
        temporary = RUN_DIR / f".{record['id']}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def run(self, mode: str, task_ids: list[str], trials: int = 1, *, run_id: str | None = None, fixture_id: str = "base", cancel_event: Event | None = None, request_limit: int | None = None, model: str | None = None, model_config: Settings | None = None, baseline_run_id: str | None = None) -> dict[str, Any]:
        indexed = self._validate(mode, task_ids, trials)
        _task_fixture_hash([indexed[task_id] for task_id in task_ids], fixture_id)
        with fixture_engine(fixture_id) as engine:
            return self._run_on_engine(mode, task_ids, trials, engine, run_id=run_id, fixture_id=fixture_id, cancel_event=cancel_event, request_limit=request_limit, model=model, model_config=model_config, baseline_run_id=baseline_run_id)

    def _run_on_engine(self, mode: str, task_ids: list[str], trials: int, engine: DuckDbEngine, *, run_id: str | None, fixture_id: str, cancel_event: Event | None = None, request_limit: int | None = None, model: str | None = None, model_config: Settings | None = None, baseline_run_id: str | None = None) -> dict[str, Any]:
        indexed = self._validate(mode, task_ids, trials)
        request_limit = self._request_limit(mode, request_limit)
        model_config = model_config or resolve_model(mode, model)
        catalog = self.catalog()
        service = self._live_service(model_config) if model_config else None
        if service and not service.model_client.enabled:
            raise ValueError("实时评测需要配置 LLM_API_KEY")
        if service:
            service.model_client.configure_request_budget(request_limit)
            service.model_client.telemetry_enabled = True
            service.workflow.stage_telemetry_enabled = True
            service.workflow.stage_telemetry = []
            service.workflow.database_engine = engine
        run_id = run_id or uuid.uuid4().hex[:12]
        total = sum(1 if indexed[task_id].get("kind") == "security_probe" else trials for task_id in task_ids)
        record: dict[str, Any] = {"id": run_id, "mode": mode, "trials": trials, "fixture_id": fixture_id, "status": "running", "task_count": len(task_ids), "model_task_count": sum(indexed[t].get("kind") != "security_probe" for t in task_ids), "task_version": catalog["version"], "fixture_hash": _task_fixture_hash([indexed[task_id] for task_id in task_ids], fixture_id), "oracle_hash": _oracle_hash(), "provenance": provenance_snapshot(mode, service.config if service else None), "review": self.review_summary(task_ids), "model": service.config.llm_model if service else "offline-demo", "started_at": datetime.now(timezone.utc).isoformat(), "summary": {"total": len(task_ids), "passed": 0, "completion": 0, "safety": 0, "process": 0, "pass3": None}, "progress": {"completed_attempts": 0, "total_attempts": total, "current_task": None}, "cases": []}
        record["evaluation_users"] = {task_id: evaluation_user_id(indexed[task_id]) for task_id in task_ids}
        record["request_budget"] = {"limit": request_limit, "used": 0, "exhausted": False, "tracked": True} if request_limit else None
        record["grader_hash"] = grader_hash()
        if run_id and (RUN_DIR / f"{run_id}.json").exists():
            queued = self.get_run(run_id)
            record["field_targets"], record["field_targets_hash"] = queued.get("field_targets", {}), queued.get("field_targets_hash")
        else:
            record["field_targets"], record["field_targets_hash"] = load_field_targets(FIELD_TARGET_FILE, catalog["version"])
        record["task_ids"] = list(task_ids)
        if baseline_run_id:
            record["baseline_run_id"] = baseline_run_id
        self._save(record)
        if service:
            def persist_request_count(count: int) -> None:
                record["request_budget"]["used"] = count
                self._save(record)
            service.model_client.on_request_reserved = persist_request_count
        for task_id in task_ids:
            if cancel_event and cancel_event.is_set():
                return self._finish_cancelled(record)
            task = indexed[task_id]
            record["progress"]["current_task"] = task_id
            self._save(record)
            expected = [] if task.get("kind") == "security_probe" else self._oracle(task, engine)
            attempts = []
            planned_trials = 1 if task.get("kind") == "security_probe" else trials
            for trial in range(planned_trials):
                if cancel_event and cancel_event.is_set():
                    break
                started = time.monotonic()
                telemetry_start = len(service.model_client.telemetry) if service else 0
                stage_start = len(service.workflow.stage_telemetry) if service else 0
                try:
                    raw = self._execute_case(task, mode, service, f"eval-{run_id}-{task_id}-{trial}", expected, engine)
                    verdict = grade(task, raw, expected)
                    error = None
                except Exception as exc:
                    raw, error = {}, f"{type(exc).__name__}: {exc}"
                    verdict = {"checks": {"execution": False}, "passed": False, "dimensions": {"completion": False, "safety": False, "process": False}}
                calls = service.model_client.telemetry[telemetry_start:] if service else []
                stages = service.workflow.stage_telemetry[stage_start:] if service else []
                if service:
                    record["request_budget"]["used"] = service.model_client.request_count
                    record["request_budget"]["exhausted"] = service.model_client.request_budget_exhausted
                schema_fields = score_attempt_fields(record["field_targets"].get(task_id), raw) if mode == "live" else None
                sql_outcome = score_sql_attempt(task, raw, verdict, mode)
                attempts.append({"trial": trial + 1, "result": raw, "grade": verdict, "error": error, "model_calls": calls, "stages": stages, "metrics": attempt_metrics((time.monotonic() - started) * 1000, calls), "schema_fields": schema_fields, "sql_outcome": sql_outcome})
                record["progress"]["completed_attempts"] += 1
                record["progress"]["current_trial"] = trial + 1
                self._save(record)
                if service and service.model_client.request_budget_exhausted:
                    break
            if attempts:
                record["cases"].append({"task_id": task_id, "category": task["category"], "query": task["query"], "kind": task.get("kind", "model"), "attempts": attempts, "incomplete": len(attempts) < planned_trials, "metrics": aggregate_metrics(attempts), "stage_metrics": aggregate_stages([stage for attempt in attempts for stage in attempt["stages"]]), "result": attempts[0]["result"], "grade": combine_attempts(attempts), "error": next((a["error"] for a in attempts if a["error"]), None), "pass3": len(attempts) == 3 and all(a["grade"]["passed"] for a in attempts)})
                self._summarize(record)
                self._save(record)
            if service and service.model_client.request_budget_exhausted:
                return self._finish_budget_exhausted(record)
            if cancel_event and cancel_event.is_set():
                return self._finish_cancelled(record)
        with self._lock:
            if cancel_event and cancel_event.is_set():
                return self._finish_cancelled(record)
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            record["status"] = "completed"
            record["progress"]["current_task"] = None
            self._summarize(record)
            self._save(record)
        return record

    def _execute_case(self, task: dict, mode: str, service: AskDataService | None, session_id: str, expected: list[dict], engine: DuckDbEngine | None = None) -> dict:
        sql = oracle_sql(task["id"])
        if task.get("kind") == "security_probe":
            return asdict((engine or self.engine).execute(task["database"], sql or "", AccessController().resolve(task["user_id"])))
        if mode == "demo":
            final = {"route": task["route"], "status": "completed", "sql": sql, "rows": expected, "tool_calls": ([{"tool": f"query_{task['database']}", "reason": "离线样例"}] if sql else []), "execution_log": [{"stage": "demo", "success": True}], "analysis": "离线样例输出，仅演示评测流程"}
            if task.get("kind") == "clarification":
                return {"initial": {"route": "database_query", "status": "waiting_clarification", "clarification": {"options": [{"id": task["option_id"], "label": "当前订单"}, {"id": "orders_history", "label": "历史订单"}]}}, "resumed": final}
            return final
        assert service is not None
        user_id = evaluation_user_id(task)
        initial = service.submit(task["query"], session_id, user_id=user_id)
        if task.get("kind") == "clarification":
            resumed = service.clarify(initial.task_id, task["option_id"], user_id=user_id) if initial.status == "waiting_clarification" and initial.clarification and task["option_id"] in {o.id for o in initial.clarification.options} else initial
            return {"initial": initial.model_dump(mode="json"), "resumed": resumed.model_dump(mode="json")}
        return initial.model_dump(mode="json")

    @staticmethod
    def _summarize(record: dict) -> None:
        cases = record["cases"]
        for case in cases:
            for attempt in case.get("attempts", []):
                if "stages" in attempt:
                    attempt["failure_attribution"] = attribute_failure(attempt)
            known_attempts = [attempt for attempt in case.get("attempts", []) if "failure_attribution" in attempt]
            if known_attempts:
                first_failed = next((attempt for attempt in known_attempts if attempt["failure_attribution"]), None)
                case["failure_attribution"] = ({"trial": first_failed["trial"], **first_failed["failure_attribution"]} if first_failed else None)
        complete_cases = [case for case in cases if not case.get("incomplete")]
        record["summary"] = {"total": record.get("task_count", len(cases)), "passed": sum(all(a["grade"]["passed"] for a in c.get("attempts", [{"grade": c["grade"]}])) for c in complete_cases), "completion": sum(all(a["grade"]["dimensions"]["completion"] for a in c.get("attempts", [{"grade": c["grade"]}])) for c in complete_cases), "safety": sum(all(a["grade"]["dimensions"]["safety"] for a in c.get("attempts", [{"grade": c["grade"]}])) for c in complete_cases), "process": sum(all(a["grade"]["dimensions"]["process"] for a in c.get("attempts", [{"grade": c["grade"]}])) for c in complete_cases), "pass3": sum(c.get("pass3", False) for c in complete_cases) if record.get("trials") == 3 else None}
        record["summary"]["metrics"] = aggregate_metrics([attempt for case in cases for attempt in case.get("attempts", [])])
        record["summary"]["schema_fields"] = aggregate_schema_fields([attempt.get("schema_fields") for case in cases for attempt in case.get("attempts", [])]) if record.get("mode") == "live" else None
        record["summary"]["sql_outcome"] = aggregate_sql_attempts([attempt.get("sql_outcome") for case in cases for attempt in case.get("attempts", [])]) if record.get("mode") == "live" else None
        record["summary"]["sql_latency"] = aggregate_sql_latencies(cases, record.get("mode", "demo"))
        record["summary"]["stage_metrics"] = aggregate_stages([stage for case in cases for attempt in case.get("attempts", []) for stage in attempt.get("stages", [])])
        record["summary"]["failure_summary"] = failure_summary(cases) if any("failure_attribution" in attempt for case in cases for attempt in case.get("attempts", [])) else None

    def list_runs(self) -> list[dict]:
        if not RUN_DIR.exists():
            return []
        result = []
        for path in sorted(RUN_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            run = json.loads(path.read_text(encoding="utf-8"))
            item = {key: run[key] for key in ("id", "mode", "model", "fixture_id", "started_at", "status", "progress", "summary", "request_budget", "grader_hash", "baseline_run_id") if key in run}
            if item.get("id") == self._active_run_id and item.get("status") in {"queued", "running"} and self._cancel_event and self._cancel_event.is_set():
                item["status"] = "cancel_requested"
            result.append(item)
        return result

    def get_run(self, run_id: str, replay: bool = False) -> dict:
        if not run_id.isalnum() or len(run_id) != 12:
            raise FileNotFoundError(run_id)
        record = json.loads((RUN_DIR / f"{run_id}.json").read_text(encoding="utf-8"))
        if record.get("status") in {"queued", "running", "cancel_requested"} and self._active_run_id != run_id and not _campaign_run_active(run_id):
            record["status"] = "interrupted"
            record["error"] = "服务重启或后台进程中断"
            self._save(record)
        elif record.get("status") in {"queued", "running"} and self._active_run_id == run_id and self._cancel_event and self._cancel_event.is_set():
            record["status"] = "cancel_requested"
        if replay:
            if record.get("status") != "completed":
                raise ValueError("运行未完成，不能重放")
            indexed = {t["id"]: t for t in self.catalog()["tasks"]}
            fixture_id = record.get("fixture_id", "base")
            record_tasks = [indexed[task_id] for task_id in record.get("task_ids", [case["task_id"] for case in record["cases"]])]
            if record["fixture_hash"] != _task_fixture_hash(record_tasks, fixture_id) or record["task_version"] != self.catalog()["version"] or record.get("oracle_hash") != _oracle_hash():
                raise ValueError("任务、标准答案或数据版本已改变，不能重放")
            with fixture_engine(fixture_id) as engine:
                for case in record["cases"]:
                    task = indexed[case["task_id"]]
                    expected = [] if task.get("kind") == "security_probe" else self._oracle(task, engine)
                    for attempt in case.get("attempts", []):
                        if not attempt["error"]:
                            attempt["grade"] = grade(task, attempt["result"], expected)
                    if case.get("attempts"):
                        case["result"] = case["attempts"][0]["result"]
                        case["grade"] = combine_attempts(case["attempts"])
                        case["error"] = next((a["error"] for a in case["attempts"] if a["error"]), None)
                        case["pass3"] = len(case["attempts"]) == 3 and all(a["grade"]["passed"] for a in case["attempts"])
                    elif not case["error"]:
                        case["grade"] = grade(task, case["result"], expected)
            self._summarize(record)
            record["replayed_at"] = datetime.now(timezone.utc).isoformat()
            record["replay_grader_hash"] = grader_hash()
        return record
