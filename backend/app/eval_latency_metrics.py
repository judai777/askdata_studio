"""真实 SQL 试验的端到端与数据库工具耗时分布。"""
from __future__ import annotations

import math
from typing import Any


def _duration(value: Any) -> float | None:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _distribution(values: list[float]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "mean_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean_ms": round(sum(ordered) / len(ordered), 2),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 2),
    }


def aggregate_sql_latencies(cases: list[dict[str, Any]], mode: str) -> dict[str, Any] | None:
    if mode != "live":
        return None
    attempts = [attempt for case in cases for attempt in case.get("attempts", []) if attempt.get("sql_outcome") is not None]
    end_to_end = []
    database_tool = []
    for attempt in attempts:
        elapsed = _duration((attempt.get("metrics") or {}).get("elapsed_ms"))
        if elapsed is not None:
            end_to_end.append(elapsed)
        for stage in attempt.get("stages") or []:
            if stage.get("stage") == "mcp_tool_call" and str(stage.get("tool") or "").startswith("query_"):
                elapsed = _duration(stage.get("elapsed_ms"))
                if elapsed is not None:
                    database_tool.append(elapsed)
    return {"sql_attempts": len(attempts), "end_to_end": _distribution(end_to_end), "database_tool": _distribution(database_tool)}
