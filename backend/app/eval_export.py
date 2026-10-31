"""评测运行的字段白名单导出。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SAFE_CONFIG_FIELDS = frozenset({
    "llm_model", "embedding_model", "embedding_dimensions", "rerank_model", "endpoint_hash",
    "temperature", "timeout", "max_retries", "schema_recall_threshold",
    "bm25_top_k", "dense_top_k", "rrf_top_k", "max_schema_fields",
    "mcp_max_tool_calls", "context_table_row_limit", "route_context_turns",
    "short_term_summary_enabled", "short_term_summary_trigger_tokens",
    "short_term_summary_batch_tokens", "short_term_min_recent_turns",
    "session_archive_enabled",
})
METRIC_FIELDS = ("elapsed_ms", "model_calls", "failed_calls", "usage_calls", "successful_calls", "total_tokens")
GRADE_DIMENSIONS = ("completion", "safety", "process")
GRADE_CHECKS = frozenset({"unauthorized_access_denied", "clarification", "route", "result", "sql_tables", "metric_fields", "time_fields", "safety", "execution"})
STAGE_NAMES = frozenset({"preprocess", "respond_directly", "answer_qa", "retrieve_schema", "prepare_single_database", "execute_single_database", "run_multi_database", "mcp_tool_call"})


def _pick(source: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if source is None:
        return None
    return {field: source[field] for field in fields if field in source}


def _grade(source: dict[str, Any]) -> dict[str, Any]:
    checks = source.get("checks") or {}
    dimensions = source.get("dimensions") or {}
    return {
        "passed": bool(source.get("passed")),
        "checks": {key: value for key, value in checks.items() if key in GRADE_CHECKS and isinstance(value, bool)},
        "dimensions": {key: bool(dimensions[key]) for key in GRADE_DIMENSIONS if key in dimensions},
    }


def _stages(source: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if source is None:
        return None
    return [{
        "stage": stage["stage"],
        "calls": stage.get("calls") if type(stage.get("calls")) is int else None,
        "failures": stage.get("failures") if type(stage.get("failures")) is int else None,
        "elapsed_ms": stage.get("elapsed_ms") if type(stage.get("elapsed_ms")) in {int, float} else None,
    } for stage in source if stage.get("stage") in STAGE_NAMES]


def _metrics(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {field: source[field] if type(source.get(field)) in {int, float} else None for field in METRIC_FIELDS}


def _safe_numbers(source: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, int | float | None] | None:
    if source is None:
        return None
    return {field: source[field] if type(source.get(field)) in {int, float} else None for field in fields}


def _safe_sql_outcome(source: dict[str, Any] | None) -> dict[str, bool | None] | None:
    if source is None:
        return None
    return {field: source[field] if type(source.get(field)) is bool else None for field in ("executed", "result_correct")}


def _safe_latency(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "sql_attempts": source.get("sql_attempts") if type(source.get("sql_attempts")) is int else None,
        "end_to_end": _safe_numbers(source.get("end_to_end"), ("count", "mean_ms", "p95_ms")),
        "database_tool": _safe_numbers(source.get("database_tool"), ("count", "mean_ms", "p95_ms")),
    }


def _attribution(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if source is None or source.get("kind") not in {"stage", "grader", "runner"}:
        return None
    stage = source.get("stage")
    checks = source.get("failed_checks") or []
    result = {
        "kind": source["kind"],
        "stage": stage if stage in STAGE_NAMES else None,
        "failed_checks": [name for name in checks if name in GRADE_CHECKS],
    }
    if type(source.get("trial")) is int:
        result["trial"] = source["trial"]
    return result


def export_report(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("status") not in {"completed", "cancelled", "budget_exhausted"}:
        raise ValueError("只能导出已完成、已取消或预算耗尽的运行")
    provenance = record.get("provenance") or {}
    config = provenance.get("config")
    cases = []
    for case in record.get("cases", []):
        attempts = [{
            "trial": attempt.get("trial"),
            "grade": _grade(attempt.get("grade") or {}),
            "metrics": _metrics(attempt.get("metrics")),
            "schema_fields": _safe_numbers(attempt.get("schema_fields"), ("target_count", "retrieved_count", "matched_count", "recall", "precision")),
            "sql_outcome": _safe_sql_outcome(attempt.get("sql_outcome")),
            "stage_metrics": _stages(_aggregate_attempt_stages(attempt.get("stages"))),
            **({"failure_attribution": _attribution(attempt.get("failure_attribution"))} if "failure_attribution" in attempt else {}),
        } for attempt in case.get("attempts", [])]
        cases.append({
            "task_id": case.get("task_id"), "category": case.get("category"),
            "kind": case.get("kind"), "incomplete": bool(case.get("incomplete")),
            "pass3": bool(case.get("pass3")), "grade": _grade(case.get("grade") or {}),
            "metrics": _metrics(case.get("metrics")),
            "stage_metrics": _stages(case.get("stage_metrics")), "attempts": attempts,
            **({"failure_attribution": _attribution(case.get("failure_attribution"))} if "failure_attribution" in case else {}),
        })
    summary = record.get("summary") or {}
    return {
        "format_version": "1.0", "exported_at": datetime.now(timezone.utc).isoformat(),
        "run": {**(_pick(record, ("id", "mode", "status", "started_at", "finished_at", "trials", "model", "task_version", "fixture_id", "fixture_hash", "oracle_hash", "grader_hash", "model_task_count", "baseline_run_id")) or {}), "task_ids": [task_id for task_id in (record.get("task_ids") or []) if isinstance(task_id, str)] if "task_ids" in record else None},
        "provenance": {
            "kind": provenance.get("kind"), "prompt_hash": provenance.get("prompt_hash"),
            "workflow_hash": provenance.get("workflow_hash"),
            "config": {key: value for key, value in config.items() if key in SAFE_CONFIG_FIELDS and isinstance(value, (str, int, float, bool))} if isinstance(config, dict) else None,
        },
        "review": _pick(record.get("review"), ("total", "approved", "rejected", "pending", "stale")),
        "request_budget": _pick(record.get("request_budget"), ("limit", "used", "exhausted")),
        "summary": {**(_pick(summary, ("total", "passed", "completion", "safety", "process", "pass3")) or {}),
                    "metrics": _metrics(summary.get("metrics")),
                    "schema_fields": _safe_numbers(summary.get("schema_fields"), ("labeled_tasks", "unlabeled_tasks", "macro_recall", "macro_precision")),
                    "sql_outcome": _safe_numbers(summary.get("sql_outcome"), ("attempts", "executed", "result_correct")),
                    "sql_latency": _safe_latency(summary.get("sql_latency")),
                    "stage_metrics": _stages(summary.get("stage_metrics")),
                    **({"failure_summary": [{"kind": item["kind"], "stage": item.get("stage") if item.get("stage") in STAGE_NAMES else None, "count": item["count"]} for item in (summary.get("failure_summary") or []) if item.get("kind") in {"stage", "grader", "runner"} and type(item.get("count")) is int]} if "failure_summary" in summary else {})},
        "cases": cases,
    }


def _aggregate_attempt_stages(stages: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not stages:
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for stage in stages:
        if stage.get("stage") in STAGE_NAMES:
            grouped.setdefault(stage["stage"], []).append(stage)
    return [{
        "stage": name, "calls": len(items),
        "failures": sum(item.get("success") is False for item in items),
        "elapsed_ms": round(sum(item["elapsed_ms"] for item in items), 2)
        if all(isinstance(item.get("elapsed_ms"), (int, float)) for item in items) else None,
    } for name, items in sorted(grouped.items())]
