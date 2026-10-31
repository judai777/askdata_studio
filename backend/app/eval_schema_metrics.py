"""字段级 Schema 召回和准确率的独立计算协议。"""
from __future__ import annotations

from typing import Any


def _field_key(database: str, table: str, field: str) -> str:
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
