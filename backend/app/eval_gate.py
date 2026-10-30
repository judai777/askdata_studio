"""无需模型的评测回归门槛。运行：python -m app.eval_gate。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import BASE_DIR
from .evaluation import EvaluationService
from .querying.duckdb_engine import DATABASE_ROOT


BASELINE_FILE = BASE_DIR / "evals" / "offline_gate.json"
REQUIRED_CSV = frozenset({"customers.csv", "products.csv", "orders_current.csv", "orders_history.csv", "sales_targets.csv"})


def check_offline_gate(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    errors = []
    for field in ("task_version", "task_hash", "mutant_version", "mutant_hash", "oracle_hash"):
        if report.get(field) != baseline.get(field):
            errors.append(f"{field} 已变化：基线 {baseline.get(field)}，当前 {report.get(field)}")
    for fixture_id, expected_hash in (baseline.get("fixture_hashes") or {}).items():
        actual_hash = (report.get("fixture_hashes") or {}).get(fixture_id)
        if actual_hash != expected_hash:
            errors.append(f"fixture {fixture_id} 指纹已变化：基线 {expected_hash}，当前 {actual_hash}")

    cases = report.get("cases") or []
    minimum = baseline.get("minimum_mutants", 0)
    if not isinstance(minimum, int) or len(cases) < minimum:
        errors.append(f"错误 SQL 样例不足：当前 {len(cases)}，最低要求 {minimum}")
    for fixture_id, counts in (report.get("counts") or {}).items():
        if fixture_id == "overall":
            continue
        if counts.get("invalid", 0):
            errors.append(f"fixture {fixture_id} 有 {counts['invalid']} 条无效 SQL")
    required_fixture = baseline.get("required_kill_fixture")
    if required_fixture not in (report.get("fixture_hashes") or {}):
        errors.append(f"缺少要求识别全部错误样例的 fixture：{required_fixture}")
    else:
        missed = [case["id"] for case in cases if case.get("fixtures", {}).get(required_fixture, {}).get("status") != "killed"]
        if missed:
            errors.append(f"fixture {required_fixture} 未识别 {len(missed)} 条错误 SQL：{', '.join(missed)}")
    return errors


def main() -> int:
    database_dir = DATABASE_ROOT / "askdata_mock"
    missing = sorted(REQUIRED_CSV - {path.name for path in database_dir.glob("*.csv")})
    if missing:
        print(f"离线门槛失败：缺少示例 CSV：{', '.join(missing)}。请先在 backend 目录运行 python seed_demo_data.py。")
        return 1
    try:
        baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        report = EvaluationService().audit()
        errors = check_offline_gate(report, baseline)
    except (OSError, ValueError, KeyError) as exc:
        print(f"离线门槛失败：{type(exc).__name__}: {exc}")
        return 1
    if errors:
        print("离线门槛失败：")
        for error in errors:
            print(f"- {error}")
        print("请核对题目、oracle、fixture 与错误 SQL 的变化，再审查并更新 evals/offline_gate.json。")
        return 1
    fixture_id = baseline["required_kill_fixture"]
    print(f"离线门槛通过：任务 v{report['task_version']}；{len(report['cases'])} 条错误 SQL 在 {fixture_id} 全部识别，三套 fixture 无无效 SQL。")
    print("该结果只覆盖已登记的错误样例，不代表模型准确率。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
