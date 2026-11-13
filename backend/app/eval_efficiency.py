"""Aggregate known model usage by workflow stage without inferring missing costs."""
from __future__ import annotations

from typing import Any


KNOWN_STAGES = frozenset({"preprocess", "respond_directly", "answer_qa", "retrieve_schema",
                          "prepare_single_database", "execute_single_database", "run_multi_database"})


def _tokens(call: dict[str, Any]) -> int | None:
    usage = call.get("usage") or {}
    total = usage.get("total_tokens")
    if total is None:
        inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
        outputs = usage.get("output_tokens", usage.get("completion_tokens"))
        if type(inputs) is int and type(outputs) is int and inputs >= 0 and outputs >= 0:
            total = inputs + outputs
    return total if type(total) is int and total >= 0 else None


def _retries(call: dict[str, Any]) -> int:
    attempts = call.get("attempts", 1)
    return max(attempts - 1, 0) if type(attempts) is int else 0


def stage_usage(attempts: list[dict[str, Any]], request_count: int | None = None) -> dict[str, Any] | None:
    calls = [call for attempt in attempts for call in (attempt.get("model_calls") or []) if isinstance(call, dict)]
    if not calls:
        if type(request_count) is int and request_count > 0:
            return {"calls": 0, "usage_calls": 0, "retry_requests": 0,
                    "unattributed_requests": request_count, "known_tokens": 0,
                    "total_tokens": None, "stages": []}
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        stage = call.get("stage")
        grouped.setdefault(stage if stage in KNOWN_STAGES else "unknown", []).append(call)
    known_tokens = sum(_tokens(call) or 0 for call in calls)
    usage_calls = sum(_tokens(call) is not None for call in calls)
    retry_requests = sum(_retries(call) for call in calls)
    unattributed_requests = max(request_count - len(calls) - retry_requests, 0) if type(request_count) is int else 0
    complete = usage_calls == len(calls) and retry_requests == 0 and unattributed_requests == 0
    stages = []
    for stage, entries in sorted(grouped.items()):
        stage_known = sum(_tokens(call) or 0 for call in entries)
        stage_covered = sum(_tokens(call) is not None for call in entries)
        stages.append({
            "stage": stage, "calls": len(entries),
            "failed_calls": sum(call.get("success") is False for call in entries),
            "retry_requests": sum(_retries(call) for call in entries),
            "usage_calls": stage_covered, "known_tokens": stage_known,
            "token_share": stage_known / known_tokens if complete and known_tokens else None,
        })
    return {"calls": len(calls), "usage_calls": usage_calls, "retry_requests": retry_requests,
            "unattributed_requests": unattributed_requests,
            "known_tokens": known_tokens, "total_tokens": known_tokens if complete else None,
            "stages": stages}


def compare_stage_usage(baseline: dict[str, Any] | None, candidate: dict[str, Any] | None,
                        *, allow_delta: bool = True) -> dict[str, Any]:
    before = baseline or None
    after = candidate or None
    complete = (allow_delta and before is not None and after is not None
                and type(before.get("total_tokens")) is int and type(after.get("total_tokens")) is int)
    stage_deltas = []
    if complete:
        before_stages = {item["stage"]: item["known_tokens"] for item in before.get("stages", [])}
        after_stages = {item["stage"]: item["known_tokens"] for item in after.get("stages", [])}
        stage_deltas = [{"stage": stage, "baseline_tokens": before_stages.get(stage, 0),
                         "candidate_tokens": after_stages.get(stage, 0),
                         "delta_tokens": after_stages.get(stage, 0) - before_stages.get(stage, 0)}
                        for stage in sorted(before_stages.keys() | after_stages.keys())]
    delta = after["total_tokens"] - before["total_tokens"] if complete else None
    return {
        "baseline": before, "candidate": after,
        "delta_tokens": delta,
        "delta_percent": delta / before["total_tokens"] if complete and before["total_tokens"] else None,
        "stage_deltas": stage_deltas,
    }
