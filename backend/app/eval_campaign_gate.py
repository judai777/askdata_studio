"""人工批准的真实评测基线与逐题回归门槛。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evaluation
from .eval_campaign import _save, _summary
from .eval_efficiency import compare_stage_usage


def _campaign(campaign_id: str, directory: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{12}", campaign_id):
        raise FileNotFoundError(campaign_id)
    return json.loads((directory / f"{campaign_id}.json").read_text(encoding="utf-8"))


def _cases(campaign: dict[str, Any], service: evaluation.EvaluationService) -> dict[str, bool]:
    if campaign["plan"]["mode"] != "live" or campaign["status"] != "completed":
        raise ValueError("只有已完成的真实模型作业可用于回归门槛")
    result: dict[str, bool] = {}
    plan = campaign["plan"]
    for shard in campaign["shards"]:
        if shard["status"] != "completed" or not shard["run_ids"]:
            raise ValueError("作业包含未完成片")
        run = service.get_run(shard["run_ids"][-1])
        if run["status"] != "completed" or run.get("mode") != "live":
            raise ValueError("作业关联的运行未完成")
        for key in ("task_version", "oracle_hash", "grader_hash", "field_targets_hash"):
            if run.get(key) != plan.get(key):
                raise ValueError("作业关联运行的评测版本不一致")
        if run.get("evaluation_users") != {task_id: plan["evaluation_users"][task_id] for task_id in shard["task_ids"]}:
            raise ValueError("作业关联运行的评测身份不一致")
        if run.get("fixture_id") != plan["fixture_id"] or run.get("trials") != plan["trials"]:
            raise ValueError("作业关联运行的实验条件不一致")
        if run.get("fixture_hash") != plan["fixture_hash"]:
            raise ValueError("作业关联运行的数据指纹不一致")
        run_source = run.get("provenance") or {}
        if run_source.get("prompt_hash") != plan["provenance"].get("prompt_hash") or run_source.get("workflow_hash") != plan["provenance"].get("workflow_hash"):
            raise ValueError("作业关联运行的实现来源不一致")
        if run_source.get("prompt_variant", "default") != plan.get("prompt_variant", "default"):
            raise ValueError("作业关联运行的 Prompt 版本不一致")
        if run_source.get("config") != plan["provenance"].get("config"):
            raise ValueError("作业关联运行的模型或参数不一致")
        if set(run.get("task_ids", [])) != set(shard["task_ids"]) or len(run.get("cases", [])) != len(shard["task_ids"]):
            raise ValueError("作业关联运行缺少题目")
        for case in run["cases"]:
            task_id = case["task_id"]
            if task_id not in shard["task_ids"] or task_id in result:
                raise ValueError("作业关联运行的题目不匹配")
            result[task_id] = bool(case["grade"]["passed"])
    if set(result) != set(plan["task_ids"]):
        raise ValueError("作业缺少题目结果")
    return result


def _baseline_dir(directory: Path) -> Path:
    return directory / "_baselines"


def list_baselines(directory: Path | None = None) -> list[dict[str, Any]]:
    root = _baseline_dir(directory or evaluation.RUN_DIR / "_campaigns")
    if not root.exists():
        return []
    return sorted((json.loads(path.read_text(encoding="utf-8")) for path in root.glob("*.json")),
                  key=lambda item: item["approved_at"], reverse=True)


def approve_baseline(campaign_id: str, fingerprint: str, note: str, reviewer: str, *,
                     directory: Path | None = None, service: evaluation.EvaluationService | None = None) -> dict[str, Any]:
    if not note.strip() or len(note.strip()) > 1000:
        raise ValueError("请填写不超过 1000 字的基线审核意见")
    root = directory or evaluation.RUN_DIR / "_campaigns"
    campaign = _campaign(campaign_id, root)
    if campaign["plan_hash"] != fingerprint:
        raise ValueError("作业计划已变化，请刷新后审核")
    cases = _cases(campaign, service or evaluation.EvaluationService())
    path = _baseline_dir(root) / f"{campaign_id}.json"
    if path.exists():
        raise ValueError("该作业已批准为基线；请创建新作业进行新的基线审核")
    record = {
        "campaign_id": campaign_id, "plan_hash": fingerprint,
        "approved_at": datetime.now(timezone.utc).isoformat(), "reviewer": reviewer,
        "note": note.strip(), "task_count": len(cases),
        "passed": sum(cases.values()), "passed_task_ids": sorted(task_id for task_id, passed in cases.items() if passed),
    }
    _save(path, record)
    return record


def compare_to_baseline(baseline_id: str, candidate_id: str, *, directory: Path | None = None,
                        service: evaluation.EvaluationService | None = None) -> dict[str, Any]:
    root = directory or evaluation.RUN_DIR / "_campaigns"
    approved = json.loads((_baseline_dir(root) / f"{baseline_id}.json").read_text(encoding="utf-8")) if re.fullmatch(r"[A-Za-z0-9]{12}", baseline_id) else None
    if approved is None:
        raise FileNotFoundError(baseline_id)
    baseline = _campaign(baseline_id, root)
    candidate = _campaign(candidate_id, root)
    if baseline_id == candidate_id:
        raise ValueError("请选择不同的基线与候选作业")
    if baseline["plan_hash"] != approved["plan_hash"]:
        raise ValueError("批准的基线指纹与当前记录不一致")
    before, after = baseline["plan"], candidate["plan"]
    for key in ("task_ids", "mode", "trials", "fixture_id", "fixture_hash", "task_version",
                "oracle_hash", "grader_hash", "field_targets_hash", "request_limit", "evaluation_users"):
        if before.get(key) != after.get(key):
            raise ValueError("候选作业的题目、数据、判分或预算条件与基线不一致")
    base_source, candidate_source = before.get("provenance") or {}, after.get("provenance") or {}
    if base_source.get("workflow_hash") != candidate_source.get("workflow_hash"):
        raise ValueError("候选作业的工作流版本与基线不一致")
    base_config, candidate_config = base_source.get("config") or {}, candidate_source.get("config") or {}
    if not base_config or not candidate_config or {k: v for k, v in base_config.items() if k != "llm_model"} != {k: v for k, v in candidate_config.items() if k != "llm_model"}:
        raise ValueError("候选作业的非 Chat 模型配置与基线不一致")
    model_changed = base_config.get("llm_model") != candidate_config.get("llm_model")
    prompt_changed = (base_source.get("prompt_hash") != candidate_source.get("prompt_hash")
                      or before.get("prompt_variant", "default") != after.get("prompt_variant", "default"))
    if model_changed and prompt_changed:
        raise ValueError("一次对比只能改变 Chat 模型或 Prompt，不能同时改变")
    if candidate_source.get("prompt_variant", "default") != after.get("prompt_variant", "default"):
        raise ValueError("候选作业的 Prompt 版本与运行来源不一致")
    service = service or evaluation.EvaluationService()
    baseline_cases = _cases(baseline, service)
    if sorted(task_id for task_id, passed in baseline_cases.items() if passed) != approved["passed_task_ids"]:
        raise ValueError("批准后的基线运行结果已变化")
    candidate_cases = _cases(candidate, service)
    regressed = sorted(task_id for task_id, passed in baseline_cases.items() if passed and not candidate_cases[task_id])
    improved = sorted(task_id for task_id, passed in baseline_cases.items() if not passed and candidate_cases[task_id])
    baseline_summary = _summary(baseline, service)
    candidate_summary = _summary(candidate, service)
    baseline_retries = sum(max(len(shard["run_ids"]) - 1, 0) for shard in baseline["shards"])
    candidate_retries = sum(max(len(shard["run_ids"]) - 1, 0) for shard in candidate["shards"])
    request_known = not baseline_summary["remote_requests_uncertain"] and not candidate_summary["remote_requests_uncertain"]
    usage = compare_stage_usage(baseline_summary.get("stage_usage"), candidate_summary.get("stage_usage"),
                                allow_delta=baseline_retries == 0 and candidate_retries == 0 and request_known)
    base_schema, candidate_schema = baseline_summary.get("schema_fields") or {}, candidate_summary.get("schema_fields") or {}
    schema_comparable = (base_schema.get("labeled_tasks", 0) > 0
                         and base_schema.get("labeled_tasks") == candidate_schema.get("labeled_tasks"))
    base_sql, candidate_sql = baseline_summary.get("sql_outcome") or {}, candidate_summary.get("sql_outcome") or {}
    return {
        "baseline_id": baseline_id, "candidate_id": candidate_id,
        "passed": not regressed, "task_count": len(baseline_cases),
        "baseline_passed": sum(baseline_cases.values()), "candidate_passed": sum(candidate_cases.values()),
        "regressed": regressed, "improved": improved,
        "variable": "prompt" if prompt_changed else "chat_model" if model_changed else "repeat",
        "quality": {
            "schema": {"labeled_tasks": base_schema.get("labeled_tasks") if schema_comparable else None,
                       "baseline_recall": base_schema.get("macro_recall"), "candidate_recall": candidate_schema.get("macro_recall"),
                       "baseline_precision": base_schema.get("macro_precision"), "candidate_precision": candidate_schema.get("macro_precision"),
                       "delta_recall": candidate_schema["macro_recall"] - base_schema["macro_recall"] if schema_comparable and base_schema.get("macro_recall") is not None and candidate_schema.get("macro_recall") is not None else None,
                       "delta_precision": candidate_schema["macro_precision"] - base_schema["macro_precision"] if schema_comparable and base_schema.get("macro_precision") is not None and candidate_schema.get("macro_precision") is not None else None},
            "sql": {"attempts": base_sql.get("attempts") if base_sql.get("attempts") == candidate_sql.get("attempts") else None,
                    "baseline_correct": base_sql.get("result_correct"), "candidate_correct": candidate_sql.get("result_correct"),
                    "delta_correct": candidate_sql["result_correct"] - base_sql["result_correct"] if base_sql.get("attempts", 0) > 0 and base_sql.get("attempts") == candidate_sql.get("attempts") else None},
        },
        "cost": {**usage,
                 "baseline_requests": baseline_summary["remote_requests_used"] if request_known else None,
                 "candidate_requests": candidate_summary["remote_requests_used"] if request_known else None,
                 "delta_requests": candidate_summary["remote_requests_used"] - baseline_summary["remote_requests_used"] if request_known else None,
                 "baseline_shard_retries": baseline_retries, "candidate_shard_retries": candidate_retries},
    }
