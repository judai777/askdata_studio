import csv
import json
import unittest
from pathlib import Path

import sqlglot
from sqlglot import exp

from app.eval_schema_metrics import score_schema_fields
from app.evaluation import ORACLE_FILE, TASK_FILE
from app.querying.duckdb_engine import DATABASE_ROOT


class FieldTargetDraftTests(unittest.TestCase):
    def test_drafts_cover_sql_tasks_and_reference_real_fields(self) -> None:
        path = Path(__file__).parents[1] / "evals" / "field_targets_draft.json"
        draft = json.loads(path.read_text(encoding="utf-8"))
        approved = json.loads((path.parent / "field_targets.json").read_text(encoding="utf-8"))
        catalog = json.loads(TASK_FILE.read_text(encoding="utf-8"))
        oracles = json.loads(ORACLE_FILE.read_text(encoding="utf-8"))
        self.assertEqual((draft["status"], draft["task_version"]), ("draft", catalog["version"]))
        self.assertEqual((approved["status"], approved["task_version"]), ("approved", catalog["version"]))
        self.assertEqual(approved["targets"], draft["targets"])
        expected_ids = {task["id"] for task in catalog["tasks"] if task.get("route") == "database_query" and task.get("kind") != "security_probe"}
        self.assertEqual(set(draft["targets"]), expected_ids)
        for task_id, labels in draft["targets"].items():
            with self.subTest(task_id=task_id):
                self.assertEqual(len(labels), len(set(labels)))
                self.assertIsNotNone(score_schema_fields(labels, []))
                names = set()
                for label in labels:
                    database, table, field = label.split(".")
                    path = DATABASE_ROOT / database / f"{table}.csv"
                    with path.open(encoding="utf-8-sig", newline="") as source:
                        self.assertIn(field, next(csv.reader(source)))
                    names.add(field)
                oracle_names = {column.name for column in sqlglot.parse_one(oracles[task_id], read="duckdb").find_all(exp.Column)}
                self.assertEqual(names, oracle_names)


if __name__ == "__main__":
    unittest.main()
