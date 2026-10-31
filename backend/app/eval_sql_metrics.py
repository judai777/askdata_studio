"""真实 SQL 试验的执行与结果统计。"""
from __future__ import annotations

from typing import Any


def score_sql_attempt(task: dict[str, Any], result: dict[str, Any], grade: dict[str, Any], mode: str) -> dict[str, bool] | None:
    if mode != "live" or task.get("route") != "database_query" or task.get("kind") == "security_probe":
        return None
    final = result.get("resumed") if task.get("kind") == "clarification" else result
    calls = final.get("tool_calls") or [] if isinstance(final, dict) else []
    database = task.get("database")
    executed = any(
        isinstance(call, dict)
        and (call.get("database") == database or call.get("tool") == f"query_{database}")
        and call.get("success") is True
        and isinstance(call.get("sql"), str) and bool(call["sql"].strip())
        for call in calls
    )
    return {"executed": executed, "result_correct": executed and grade.get("checks", {}).get("result") is True}


def aggregate_sql_attempts(outcomes: list[dict[str, bool] | None]) -> dict[str, int]:
    valid = [item for item in outcomes if item is not None]
    return {
        "attempts": len(valid),
        "executed": sum(item["executed"] for item in valid),
        "result_correct": sum(item["result_correct"] for item in valid),
    }
