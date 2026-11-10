"""可恢复的分片评测作业。运行：python -m app.eval_campaign --help。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evaluation
from .eval_grader_version import grader_hash
from .eval_models import resolve_model
from .eval_provenance import provenance_snapshot
from .eval_schema_metrics import load_field_targets


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
    for shard in record["shards"]:
        latest = None
        for run_id in shard["run_ids"]:
            try:
                run = service.get_run(run_id)
            except FileNotFoundError:
                continue
            used += (run.get("request_budget") or {}).get("used", 0)
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
    }
    record["summary"] = result
    return result


def _run_campaign_unlocked(
    campaign_id: str,
    task_ids: list[str],
    *,
    mode: str = "demo",
    trials: int = 1,
    fixture_id: str = "base",
    request_limit: int | None = None,
    model: str | None = None,
    allow_remote: bool = False,
    retry_interrupted: bool = False,
    service: evaluation.EvaluationService | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{12}", campaign_id):
        raise ValueError("作业 ID 必须是 12 位字母或数字")
    if not isinstance(task_ids, list) or not task_ids or any(not isinstance(task_id, str) for task_id in task_ids) or len(task_ids) != len(set(task_ids)):
        raise ValueError("作业必须包含不重复的题目 ID")
    if mode not in {"demo", "live"} or trials not in {1, 3}:
        raise ValueError("作业模式或试验次数无效")
    if mode == "demo" and (request_limit is not None or model is not None or allow_remote):
        raise ValueError("无模型作业不能设置远程调用参数")
    if mode == "live" and (not allow_remote or type(request_limit) is not int or not 1 <= request_limit <= 100):
        raise ValueError("实时作业须显式允许远程调用，并设置 1–100 次总请求预算")
    service = service or evaluation.EvaluationService()
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
    config = resolve_model(mode, model)
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
        "provenance": provenance_snapshot(mode, config),
    }
    plan_hash = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    path = (directory or evaluation.RUN_DIR / "_campaigns") / f"{campaign_id}.json"
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
    _summary(record, service)
    for shard in record["shards"]:
        if shard["status"] == "completed":
            continue
        if evaluation._task_fixture_hash(tasks, fixture_id) != plan["fixture_hash"] or evaluation._oracle_hash() != plan["oracle_hash"]:
            record["status"] = "failed"
            _save(path, record)
            raise ValueError("作业执行期间数据或标准答案已变化，请使用新作业 ID")
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
            run = service.run(mode, shard["task_ids"], trials, run_id=run_id, fixture_id=fixture_id, request_limit=remaining, model=model)
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


def _public_campaign(record: dict[str, Any]) -> dict[str, Any]:
    plan = record["plan"]
    return {
        "id": record["id"], "created_at": record["created_at"],
        "status": record["status"], "plan_hash": record["plan_hash"],
        "mode": plan["mode"], "trials": plan["trials"],
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
    path = (directory or evaluation.RUN_DIR / "_campaigns") / f"{campaign_id}.json"
    return _public_campaign(json.loads(path.read_text(encoding="utf-8")))


def list_campaigns(directory: Path | None = None) -> list[dict[str, Any]]:
    root = directory or evaluation.RUN_DIR / "_campaigns"
    if not root.exists():
        return []
    campaigns = [get_campaign(path.stem, root) for path in root.glob("*.json") if re.fullmatch(r"[A-Za-z0-9]{12}", path.stem)]
    return sorted(campaigns, key=lambda item: item["created_at"], reverse=True)


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
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--retry-interrupted", action="store_true")
    args = parser.parse_args()
    try:
        task_ids = json.loads(args.task_file.read_text(encoding="utf-8"))["task_ids"] if args.task_file else args.task_ids
        record = run_campaign(args.campaign_id, task_ids, mode=args.mode, trials=args.trials,
                              fixture_id=args.fixture_id, request_limit=args.request_limit, model=args.model,
                              allow_remote=args.allow_remote, retry_interrupted=args.retry_interrupted)
    except (ValueError, KeyError, OSError, RuntimeError, TypeError) as error:
        parser.exit(2, f"评测作业失败：{error}\n")
    print(json.dumps({"id": record["id"], "status": record["status"], "plan_hash": record["plan_hash"], "summary": record["summary"]}, ensure_ascii=False))
    return 0 if record["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
