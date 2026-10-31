"""人工轨迹标签与确定性判分分歧统计。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BASE_DIR


LABEL_FILE = BASE_DIR / "data" / "eval_labels.json"
LABEL_DIMENSIONS = ("completion", "safety", "process", "report_quality")
LABEL_VALUES = {"pass", "fail", "unsure"}
FINISHED_STATUSES = {"completed", "cancelled", "budget_exhausted"}


def attempt_fingerprint(run: dict[str, Any], task_id: str, attempt: dict[str, Any]) -> str:
    payload = {
        "run_id": run["id"], "task_version": run.get("task_version"),
        "fixture_hash": run.get("fixture_hash"), "oracle_hash": run.get("oracle_hash"),
        "task_id": task_id, "trial": attempt["trial"], "result": attempt.get("result"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def read_labels() -> dict[str, Any]:
    if not LABEL_FILE.exists():
        return {"runs": {}}
    return json.loads(LABEL_FILE.read_text(encoding="utf-8"))


def _write_labels(payload: dict[str, Any]) -> None:
    LABEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = LABEL_FILE.with_name(f".{LABEL_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, LABEL_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def _find_attempt(run: dict[str, Any], task_id: str, trial: int) -> dict[str, Any]:
    for case in run.get("cases", []):
        if case.get("task_id") == task_id:
            for attempt in case.get("attempts", []):
                if attempt.get("trial") == trial:
                    return attempt
    raise KeyError("评测试验不存在")


def labels_for_run(run: dict[str, Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    stored = (data or read_labels()).get("runs", {}).get(run["id"], {})
    items = []
    for case in run.get("cases", []):
        task_id = case["task_id"]
        for attempt in case.get("attempts", []):
            fingerprint = attempt_fingerprint(run, task_id, attempt)
            history = stored.get(task_id, {}).get(str(attempt["trial"]), [])
            latest = history[-1] if history else None
            items.append({
                "task_id": task_id, "trial": attempt["trial"], "fingerprint": fingerprint,
                "status": "unlabeled" if latest is None else "valid" if latest["fingerprint"] == fingerprint else "stale",
                "latest": latest, "history": history,
            })
    return {"run_id": run["id"], "mode": run["mode"], "items": items}


def submit_label(run: dict[str, Any], task_id: str, trial: int, fingerprint: str, labels: dict[str, str], note: str, reviewer: str) -> dict[str, Any]:
    if run.get("status") not in FINISHED_STATUSES:
        raise ValueError("只能标注已结束运行中的试验")
    if type(trial) is not int or trial < 1:
        raise ValueError("试验序号无效")
    if set(labels) != set(LABEL_DIMENSIONS) or any(value not in LABEL_VALUES for value in labels.values()):
        raise ValueError("请为四个维度选择通过、未通过或不确定")
    if not note.strip() or len(note.strip()) > 1000:
        raise ValueError("请填写 1–1000 字的人工判断依据")
    attempt = _find_attempt(run, task_id, trial)
    current = attempt_fingerprint(run, task_id, attempt)
    if fingerprint != current:
        raise RuntimeError("试验轨迹或评测依据已变化，请刷新后重新标注")
    entry = {
        "fingerprint": current, "labels": {name: labels[name] for name in LABEL_DIMENSIONS},
        "note": note.strip(), "reviewer": reviewer,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    data = read_labels()
    data.setdefault("runs", {}).setdefault(run["id"], {}).setdefault(task_id, {}).setdefault(str(trial), []).append(entry)
    _write_labels(data)
    return {"run_id": run["id"], "task_id": task_id, "trial": trial, "status": "valid", "latest": entry}


def calibration_summary(runs: list[dict[str, Any]], data: dict[str, Any] | None = None) -> dict[str, Any]:
    stored = data or read_labels()
    groups: dict[str, dict[str, Any]] = {}
    totals = {"live_attempts": 0, "labeled_attempts": 0, "stale_labels": 0, "demo_practice": 0}
    for run in runs:
        if run.get("status") not in FINISHED_STATUSES:
            continue
        labels_by_key = {(item["task_id"], item["trial"]): item for item in labels_for_run(run, stored)["items"]}
        for case in run.get("cases", []):
            for attempt in case.get("attempts", []):
                item = labels_by_key[(case["task_id"], attempt["trial"])]
                if run["mode"] == "demo":
                    totals["demo_practice"] += item["status"] == "valid"
                    continue
                totals["live_attempts"] += 1
                if item["status"] == "stale":
                    totals["stale_labels"] += 1
                if item["status"] != "valid":
                    continue
                totals["labeled_attempts"] += 1
                key = run.get("grader_hash") or "unknown"
                group = groups.setdefault(key, {"grader_hash": key, "attempts": 0, "dimensions": {name: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "unsure": 0} for name in LABEL_DIMENSIONS[:3]}, "report_quality": {"pass": 0, "fail": 0, "unsure": 0}})
                group["attempts"] += 1
                labels = item["latest"]["labels"]
                for name in LABEL_DIMENSIONS[:3]:
                    human = labels[name]
                    counts = group["dimensions"][name]
                    if human == "unsure":
                        counts["unsure"] += 1
                    else:
                        automatic = bool(attempt.get("grade", {}).get("dimensions", {}).get(name))
                        outcome = "tp" if automatic and human == "pass" else "fp" if automatic else "fn" if human == "pass" else "tn"
                        counts[outcome] += 1
                group["report_quality"][labels["report_quality"]] += 1
    for group in groups.values():
        for counts in group["dimensions"].values():
            decided = counts["tp"] + counts["fp"] + counts["fn"] + counts["tn"]
            counts["decided"] = decided
            counts["agreement"] = round((counts["tp"] + counts["tn"]) / decided, 4) if decided else None
    return {**totals, "groups": sorted(groups.values(), key=lambda group: group["grader_hash"])}
