"""可恢复的分片评测作业。运行：python -m app.eval_campaign --help。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from . import evaluation
from .eval_grader_version import grader_hash
from .eval_models import resolve_model
from .eval_provenance import provenance_snapshot
from .eval_schema_metrics import aggregate_schema_fields, load_field_targets
from .eval_sql_metrics import aggregate_sql_attempts


def _save(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _summary(record: dict[str, Any], service: evaluation.EvaluationService) -> dict[str, Any]:
    completed = []
    used = 0
    uncertain = False
    for shard in record["shards"]:
        latest = None
        for run_id in shard["run_ids"]:
            try:
                run = service.get_run(run_id)
            except FileNotFoundError:
                continue
            budget = run.get("request_budget") or {}
            used += budget.get("used", 0)
            if record["plan"]["mode"] == "live" and run.get("status") != "completed" and not budget.get("tracked"):
                uncertain = True
            latest = run
        if latest and latest.get("status") == "completed":
            completed.append(latest)
            shard["status"] = "completed"
    result = {
        "total_tasks": len(record["plan"]["task_ids"]),
        "completed_tasks": sum(run["task_count"] for run in completed),
        "passed": sum(run["summary"]["passed"] for run in completed),
        "completed_shards": len(completed),
        "total_shards": len(record["shards"]),
        "remote_requests_used": used if record["plan"]["mode"] == "live" else 0,
        "remote_requests_uncertain": uncertain,
    }
    if record["plan"]["mode"] == "live":
        attempts = [attempt for run in completed for case in run.get("cases", []) for attempt in case.get("attempts", [])]
        result["schema_fields"] = aggregate_schema_fields([attempt.get("schema_fields") for attempt in attempts])
        result["sql_outcome"] = aggregate_sql_attempts([attempt.get("sql_outcome") for attempt in attempts])
        result["model_metrics"] = evaluation.aggregate_metrics(attempts)
    record["summary"] = result
    return result


def _build_plan(campaign_id: str, task_ids: list[str], mode: str, trials: int, fixture_id: str,
                request_limit: int | None, model: str | None, prompt_variant: str, allow_remote: bool,
                service: evaluation.EvaluationService) -> tuple[dict[str, Any], list[list[str]], list[dict[str, Any]]]:
    if not re.fullmatch(r"[A-Za-z0-9]{12}", campaign_id):
        raise ValueError("作业 ID 必须是 12 位字母或数字")
    if not isinstance(task_ids, list) or not task_ids or any(not isinstance(task_id, str) for task_id in task_ids) or len(task_ids) != len(set(task_ids)):
        raise ValueError("作业必须包含不重复的题目 ID")
    if mode not in {"demo", "live"} or trials not in {1, 3}:
        raise ValueError("作业模式或试验次数无效")
    if mode == "demo" and (request_limit is not None or model is not None or prompt_variant != "default" or allow_remote):
        raise ValueError("无模型作业不能设置远程调用参数")
    if mode == "live" and (not allow_remote or type(request_limit) is not int or not 1 <= request_limit <= 100):
        raise ValueError("实时作业须显式允许远程调用，并设置 1–100 次总请求预算")
    catalog = service.catalog()
    indexed = {task["id"]: task for task in catalog["tasks"]}
    if any(task_id not in indexed for task_id in task_ids):
        raise ValueError("作业包含未知题目 ID")
    tasks = [indexed[task_id] for task_id in task_ids]
    fixture_hash = evaluation._task_fixture_hash(tasks, fixture_id)
    chunk_size = 3 if mode == "live" and trials == 3 else 5
    chunks = [task_ids[index:index + chunk_size] for index in range(0, len(task_ids), chunk_size)]
    for chunk in chunks:
        service._validate(mode, chunk, trials)
    config = resolve_model(mode, model, prompt_variant)
    if mode == "live":
        if not config or not config.api_key:
            raise ValueError("实时评测需要配置 LLM_API_KEY")
        statuses = {item["task_id"]: item["status"] for item in service.reviews()["tasks"]}
        if any(statuses[task_id] != "approved" for task_id in task_ids):
            raise ValueError("实时自动化作业只接受当前人工审核通过的题目")
    field_hash = load_field_targets(evaluation.FIELD_TARGET_FILE, catalog["version"])[1]
    plan = {
        "task_ids": task_ids, "mode": mode, "trials": trials, "fixture_id": fixture_id,
        "fixture_hash": fixture_hash, "task_version": catalog["version"],
        "oracle_hash": evaluation._oracle_hash(), "grader_hash": grader_hash(),
        "field_targets_hash": field_hash, "request_limit": request_limit,
        "evaluation_users": {task["id"]: evaluation.evaluation_user_id(task) for task in tasks},
        "prompt_variant": prompt_variant,
        "provenance": provenance_snapshot(mode, config),
    }
    return plan, chunks, tasks


def _load_or_create(campaign_id: str, plan: dict[str, Any], chunks: list[list[str]], directory: Path) -> tuple[dict[str, Any], Path]:
    plan_hash = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    path = directory / f"{campaign_id}.json"
    if path.exists():
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("plan_hash") != plan_hash:
            raise ValueError("作业参数或版本指纹已变化，请使用新作业 ID")
    else:
        record = {
            "id": campaign_id, "plan": plan, "plan_hash": plan_hash,
            "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
            "shards": [{"task_ids": chunk, "run_ids": [], "status": "pending"} for chunk in chunks],
        }
        _save(path, record)
    return record, path


def _run_campaign_unlocked(
    campaign_id: str,
    task_ids: list[str],
    *,
    mode: str = "demo",
    trials: int = 1,
    fixture_id: str = "base",
    request_limit: int | None = None,
    model: str | None = None,
    prompt_variant: str = "default",
    allow_remote: bool = False,
    retry_interrupted: bool = False,
    service: evaluation.EvaluationService | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    service = service or evaluation.EvaluationService()
    plan, chunks, tasks = _build_plan(campaign_id, task_ids, mode, trials, fixture_id, request_limit, model, prompt_variant, allow_remote, service)
    record, path = _load_or_create(campaign_id, plan, chunks, directory or evaluation.RUN_DIR / "_campaigns")
    _summary(record, service)
    for shard in record["shards"]:
        if shard["status"] == "completed":
            continue
        if (evaluation._task_fixture_hash(tasks, fixture_id) != plan["fixture_hash"]
                or evaluation._oracle_hash() != plan["oracle_hash"]
                or grader_hash() != plan["grader_hash"]
                or load_field_targets(evaluation.FIELD_TARGET_FILE, plan["task_version"])[1] != plan["field_targets_hash"]
                or provenance_snapshot(mode, resolve_model(mode, model, prompt_variant)) != plan["provenance"]):
            record["status"] = "failed"
            _save(path, record)
            raise ValueError("作业执行期间数据、判分或实现来源已变化，请使用新作业 ID")
        previous = None
        if shard["run_ids"]:
            try:
                previous = service.get_run(shard["run_ids"][-1])
            except FileNotFoundError:
                pass
        if previous and previous["status"] == "budget_exhausted":
            record["status"] = "budget_exhausted"
            _summary(record, service)
            _save(path, record)
            return record
        if previous and mode == "live" and not (previous.get("request_budget") or {}).get("tracked"):
            record["status"] = "budget_uncertain"
            _save(path, record)
            raise ValueError("旧中断片的远程请求数不可确认，不能续跑；请使用新作业 ID")
        if previous and mode == "live" and not retry_interrupted:
            record["status"] = "interrupted"
            _save(path, record)
            raise ValueError("实时作业存在未完成片；重试可能重复远程请求，须显式设置 retry_interrupted")
        remaining = request_limit - record["summary"]["remote_requests_used"] if mode == "live" else None
        if mode == "live" and remaining <= 0:
            record["status"] = "budget_exhausted"
            _save(path, record)
            return record
        run_id = uuid.uuid4().hex[:12]
        shard["run_ids"].append(run_id)
        shard["status"] = "running"
        record["status"] = "running"
        _save(path, record)
        try:
            run = service.run(mode, shard["task_ids"], trials, run_id=run_id, fixture_id=fixture_id, request_limit=remaining,
                              model_config=resolve_model(mode, model, prompt_variant))
        except Exception:
            shard["status"] = "failed"
            record["status"] = "failed"
            _summary(record, service)
            _save(path, record)
            raise
        _summary(record, service)
        if run["status"] != "completed":
            shard["status"] = run["status"]
            record["status"] = run["status"]
            _save(path, record)
            return record
        _save(path, record)
    record["status"] = "completed"
    _summary(record, service)
    _save(path, record)
    return record


def run_campaign(campaign_id: str, task_ids: list[str], **kwargs: Any) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{12}", campaign_id):
        raise ValueError("作业 ID 必须是 12 位字母或数字")
    directory = kwargs.get("directory") or evaluation.RUN_DIR / "_campaigns"
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{campaign_id}.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("该评测作业正在另一进程执行") from error
        try:
            return _run_campaign_unlocked(campaign_id, task_ids, **kwargs)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _public_campaign(record: dict[str, Any], *, status: str | None = None) -> dict[str, Any]:
    plan = record["plan"]
    return {
        "id": record["id"], "created_at": record["created_at"],
        "status": status or record["status"], "plan_hash": record["plan_hash"],
        "mode": plan["mode"], "trials": plan["trials"],
        "prompt_variant": plan.get("prompt_variant", "default"),
        "task_ids": plan["task_ids"], "request_limit": plan["request_limit"],
        "model": (plan.get("provenance", {}).get("config") or {}).get("llm_model"),
        "evaluation_users": plan.get("evaluation_users"),
        "fixture_id": plan["fixture_id"], "task_version": plan["task_version"],
        "summary": record.get("summary"),
        "shards": [
            {"task_ids": shard["task_ids"], "status": shard["status"], "run_ids": shard["run_ids"]}
            for shard in record["shards"]
        ],
    }


def get_campaign(campaign_id: str, directory: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{12}", campaign_id):
        raise FileNotFoundError(campaign_id)
    root = directory or evaluation.RUN_DIR / "_campaigns"
    path = root / f"{campaign_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["plan"]["mode"] == "live" and record["status"] in {"failed", "interrupted", "budget_uncertain"}:
        _summary(record, evaluation.EvaluationService())
    status = record["status"]
    if status in {"queued", "running"} and campaign_manager._active_id != campaign_id:
        lock_path = root / f"{campaign_id}.lock"
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                status = "interrupted"
                fcntl.flock(lock, fcntl.LOCK_UN)
    return _public_campaign(record, status=status)


def list_campaigns(directory: Path | None = None) -> list[dict[str, Any]]:
    root = directory or evaluation.RUN_DIR / "_campaigns"
    if not root.exists():
        return []
    campaigns = [get_campaign(path.stem, root) for path in root.glob("*.json") if re.fullmatch(r"[A-Za-z0-9]{12}", path.stem)]
    return sorted(campaigns, key=lambda item: item["created_at"], reverse=True)


class CampaignManager:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="askdata-campaign")
        self._lock = Lock()
        self._active_id: str | None = None

    def start(self, campaign_id: str, task_ids: list[str], *, mode: str = "demo", trials: int = 1,
              fixture_id: str = "base", request_limit: int | None = None, model: str | None = None,
              prompt_variant: str = "default", allow_remote: bool = False, retry_interrupted: bool = False) -> dict[str, Any]:
        if not isinstance(task_ids, list) or len(task_ids) > 50:
            raise ValueError("页面作业最多选择 50 题")
        service = evaluation.EvaluationService()
        plan, chunks, _ = _build_plan(campaign_id, task_ids, mode, trials, fixture_id, request_limit, model, prompt_variant, allow_remote, service)
        directory = evaluation.RUN_DIR / "_campaigns"
        with self._lock:
            if self._active_id:
                raise RuntimeError("已有自动化评测作业正在运行")
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f"{campaign_id}.lock").open("a+") as campaign_lock:
                try:
                    fcntl.flock(campaign_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RuntimeError("该评测作业正在另一进程执行") from error
                fcntl.flock(campaign_lock, fcntl.LOCK_UN)
            record, path = _load_or_create(campaign_id, plan, chunks, directory)
            _summary(record, service)
            if record["summary"]["completed_shards"] == len(chunks):
                record["status"] = "completed"
                _save(path, record)
                return _public_campaign(record)
            incomplete = next(shard for shard in record["shards"] if shard["status"] != "completed")
            if mode == "live" and incomplete["run_ids"]:
                previous = service.get_run(incomplete["run_ids"][-1])
                if not (previous.get("request_budget") or {}).get("tracked"):
                    raise ValueError("旧中断片的远程请求数不可确认，不能续跑；请使用新作业 ID")
            if mode == "live" and incomplete["run_ids"] and not retry_interrupted:
                raise ValueError("实时未完成片可能重复发送请求，请确认后重试")
            self._active_id = campaign_id
            record["status"] = "queued"
            _save(path, record)
            self._executor.submit(self._run_and_finish, campaign_id, task_ids, path, service, directory,
                                  mode, trials, fixture_id, request_limit, model, prompt_variant, allow_remote, retry_interrupted)
            return _public_campaign(record)

    def _run_and_finish(self, campaign_id: str, task_ids: list[str], path: Path,
                        service: evaluation.EvaluationService, directory: Path, mode: str, trials: int,
                        fixture_id: str, request_limit: int | None, model: str | None, prompt_variant: str,
                        allow_remote: bool, retry_interrupted: bool) -> None:
        error = False
        try:
            run_campaign(campaign_id, task_ids, mode=mode, trials=trials, fixture_id=fixture_id,
                         request_limit=request_limit, model=model, prompt_variant=prompt_variant, allow_remote=allow_remote,
                         retry_interrupted=retry_interrupted, service=service, directory=directory)
        except Exception:
            error = True
        finally:
            self._finished(campaign_id, path, error)

    def _finished(self, campaign_id: str, path: Path, error: bool) -> None:
        with self._lock:
            if error and path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                if record["status"] in {"queued", "running"}:
                    record["status"] = "failed"
                    _save(path, record)
            if self._active_id == campaign_id:
                self._active_id = None


campaign_manager = CampaignManager()


def main() -> int:
    parser = argparse.ArgumentParser(description="运行可恢复的分片评测作业")
    parser.add_argument("--campaign-id", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task-id", action="append", dest="task_ids")
    source.add_argument("--task-file", type=Path, help="包含 task_ids 数组的 JSON 文件")
    parser.add_argument("--mode", choices=("demo", "live"), default="demo")
    parser.add_argument("--trials", type=int, choices=(1, 3), default=1)
    parser.add_argument("--fixture-id", default="base")
    parser.add_argument("--request-limit", type=int)
    parser.add_argument("--model")
    parser.add_argument("--prompt-variant", default="default")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--retry-interrupted", action="store_true")
    args = parser.parse_args()
    try:
        task_ids = json.loads(args.task_file.read_text(encoding="utf-8"))["task_ids"] if args.task_file else args.task_ids
        record = run_campaign(args.campaign_id, task_ids, mode=args.mode, trials=args.trials,
                              fixture_id=args.fixture_id, request_limit=args.request_limit, model=args.model,
                              prompt_variant=args.prompt_variant,
                              allow_remote=args.allow_remote, retry_interrupted=args.retry_interrupted)
    except (ValueError, KeyError, OSError, RuntimeError, TypeError) as error:
        parser.exit(2, f"评测作业失败：{error}\n")
    print(json.dumps({"id": record["id"], "status": record["status"], "plan_hash": record["plan_hash"], "summary": record["summary"]}, ensure_ascii=False))
    return 0 if record["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
