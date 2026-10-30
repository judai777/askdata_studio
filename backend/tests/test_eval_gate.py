from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.eval_gate import BASELINE_FILE, check_offline_gate, main


class OfflineGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        fixture_ids = tuple(self.baseline["fixture_hashes"])
        self.report = {
            "task_version": self.baseline["task_version"],
            "task_hash": self.baseline["task_hash"],
            "mutant_version": self.baseline["mutant_version"],
            "mutant_hash": self.baseline["mutant_hash"],
            "oracle_hash": self.baseline["oracle_hash"],
            "fixture_hashes": self.baseline["fixture_hashes"],
            "counts": {name: {"invalid": 0} for name in fixture_ids},
            "cases": [{"id": f"mutant-{i}", "fixtures": {name: {"status": "killed"} for name in fixture_ids}} for i in range(self.baseline["minimum_mutants"])],
        }

    def test_gate_detects_version_drift_and_unreviewed_mutant_reduction(self) -> None:
        self.assertEqual(check_offline_gate(self.report, self.baseline), [])
        changed = deepcopy(self.report)
        changed["oracle_hash"] = "unreviewed"
        changed["task_hash"] = "unreviewed-task"
        changed["fixture_hashes"]["edge_v2"] = "changed"
        changed["cases"].pop()
        errors = check_offline_gate(changed, self.baseline)
        self.assertTrue(any("oracle_hash" in item for item in errors))
        self.assertTrue(any("task_hash" in item for item in errors))
        self.assertTrue(any("fixture edge_v2" in item for item in errors))
        self.assertTrue(any("样例不足" in item for item in errors))

    def test_gate_detects_invalid_and_surviving_sql_with_case_id(self) -> None:
        changed = deepcopy(self.report)
        changed["counts"]["base"]["invalid"] = 1
        changed["cases"][0]["fixtures"]["edge_v2"]["status"] = "survived"
        errors = check_offline_gate(changed, self.baseline)
        self.assertTrue(any("无效 SQL" in item for item in errors))
        self.assertTrue(any("mutant-0" in item for item in errors))

    def test_missing_demo_data_has_actionable_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.eval_gate.DATABASE_ROOT", Path(directory)):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(), 1)
        self.assertIn("seed_demo_data.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()
