"""字段级 Schema 召回和准确率的独立计算协议。"""
from __future__ import annotations

from typing import Any
import hashlib
import json
from pathlib import Path


def _field_key(database: str, table: str, field: str) -> str:
    if isinstance(database, str) and isinstance(table, str) and table.startswith(f"{database}."):
        table = table[len(database) + 1:]
    parts = (database, table, field)
    if any(not isinstance(part, str) or not part or "." in part for part in parts):
        raise ValueError("字段必须有非空且不含点号的数据库、表和字段名")
    return ".".join(parts)


def score_schema_fields(target_fields: list[str] | None, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """缺少人工核验标签时返回 None，不生成虚假的指标。"""
    if target_fields is None:
        return None
    if not target_fields:
        raise ValueError("目标字段集合不能为空")
    target: set[str] = set()
    for label in target_fields:
        if not isinstance(label, str) or len(label.split(".")) != 3:
            raise ValueError("目标字段应为 database.table.field")
        target.add(_field_key(*label.split(".")))
    retrieved = {
        _field_key(hit.get("database_id"), hit.get("table_id"), hit.get("field_name"))
        for hit in hits
    }
    matched = target & retrieved
    return {
        "target_count": len(target),
        "retrieved_count": len(retrieved),
        "matched_count": len(matched),
        "recall": len(matched) / len(target),
        "precision": len(matched) / len(retrieved) if retrieved else 0.0,
        "missing": sorted(target - retrieved),
        "extra": sorted(retrieved - target),
    }


def aggregate_schema_fields(scores: list[dict[str, Any] | None]) -> dict[str, Any]:
    valid = [score for score in scores if score is not None]
    return {
        "labeled_tasks": len(valid),
        "unlabeled_tasks": len(scores) - len(valid),
        "macro_recall": sum(score["recall"] for score in valid) / len(valid) if valid else None,
        "macro_precision": sum(score["precision"] for score in valid) / len(valid) if valid else None,
    }


def load_field_targets(path: Path, task_version: str) -> tuple[dict[str, list[str]], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "approved" or payload.get("task_version") != task_version:
        return {}, hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return payload["targets"], hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def score_attempt_fields(target_fields: list[str] | None, result: dict[str, Any]) -> dict[str, Any] | None:
    final = result.get("resumed") if "resumed" in result else result
    retrieval = final.get("retrieval") if isinstance(final, dict) else None
    if target_fields is None or not isinstance(retrieval, dict) or not isinstance(retrieval.get("hits"), list):
        return None
    return score_schema_fields(target_fields, retrieval["hits"])
