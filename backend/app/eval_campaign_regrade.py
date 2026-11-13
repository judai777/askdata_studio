"""Create an auditable local regrade of a completed live campaign without model calls."""
from __future__ import annotations

import copy
import fcntl
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evaluation
from .eval_campaign import _public_campaign, _save, _summary, plan_fingerprint
from .eval_campaign_gate import _campaign, _cases
from .eval_grader_version import grader_hash
from .eval_schema_metrics import load_field_targets
from .eval_sql_metrics import score_sql_attempt


def regrade_campaign(source_id: str, campaign_id: str, *, directory: Path | None = None,
                     service: evaluation.EvaluationService | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{12}", campaign_id) or source_id == campaign_id:
        raise ValueError("请提供不同的 12 位新作业 ID")
    service = service or evaluation.EvaluationService()
    root = directory or evaluation.RUN_DIR / "_campaigns"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{campaign_id}.json"
    with (root / f"{campaign_id}.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if target.exists():
                raise ValueError("新作业 ID 已存在")
            source = _campaign(source_id, root)
            if source.get("plan_hash") != plan_fingerprint(source["plan"]):
                raise ValueError("源作业计划指纹不一致")
            _cases(source, service)
            if source["plan"]["mode"] != "live" or any(len(shard["run_ids"]) != 1 for shard in source["shards"]):
                raise ValueError("只支持无片重试的已完成真实作业")
            current_hash = grader_hash()
            if source["plan"]["grader_hash"] == current_hash:
                raise ValueError("源作业已使用当前判分器，无需重判")
            task_version = service.catalog()["version"]
            if source["plan"]["task_version"] != task_version:
                raise ValueError("当前任务版本与源作业不同")
            field_hash = load_field_targets(evaluation.FIELD_TARGET_FILE, task_version)[1]
            if source["plan"]["field_targets_hash"] != field_hash:
                raise ValueError("当前字段标签版本与源作业不同")
            statuses = {item["task_id"]: item["status"] for item in service.reviews()["tasks"]}
            if any(statuses[task_id] != "approved" for task_id in source["plan"]["task_ids"]):
                raise ValueError("源作业题目已不再全部审核通过")
            indexed = {task["id"]: task for task in service.catalog()["tasks"]}
            prepared = []
            shards = []
            reserved_ids: set[str] = set()
            for shard in source["shards"]:
                original_id = shard["run_ids"][0]
                run = copy.deepcopy(service.get_run(original_id, replay=True))
                if run.get("replay_grader_hash") != current_hash:
                    raise ValueError("旧运行重判版本不一致")
                new_run_id = uuid.uuid4().hex[:12]
                while new_run_id in reserved_ids or (evaluation.RUN_DIR / f"{new_run_id}.json").exists():
                    new_run_id = uuid.uuid4().hex[:12]
                reserved_ids.add(new_run_id)
                run["id"] = new_run_id
                run["regraded_from"] = original_id
                run["original_grader_hash"] = run["grader_hash"]
                run["grader_hash"] = current_hash
                run["regraded_at"] = datetime.now(timezone.utc).isoformat()
                run.pop("replay_grader_hash", None)
                run.pop("replayed_at", None)
                for case in run["cases"]:
                    task = indexed[case["task_id"]]
                    for attempt in case.get("attempts", []):
                        attempt["sql_outcome"] = score_sql_attempt(task, attempt.get("result") or {}, attempt["grade"], "live")
                service._summarize(run)
                prepared.append(run)
                shards.append({"task_ids": list(shard["task_ids"]), "run_ids": [new_run_id], "status": "completed"})
            plan = copy.deepcopy(source["plan"])
            plan["grader_hash"] = current_hash
            record = {"id": campaign_id, "plan": plan, "plan_hash": plan_fingerprint(plan),
                      "created_at": datetime.now(timezone.utc).isoformat(), "status": "completed",
                      "regraded_from": source_id, "original_grader_hash": source["plan"]["grader_hash"],
                      "shards": shards}
            created = []
            try:
                for run in prepared:
                    service._save(run)
                    created.append(evaluation.RUN_DIR / f"{run['id']}.json")
                _summary(record, service)
                _save(target, record)
            except Exception:
                for path in created:
                    path.unlink(missing_ok=True)
                raise
            return _public_campaign(record)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
