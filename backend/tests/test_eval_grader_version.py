from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from app.config import BASE_DIR
from app.eval_grader_version import grader_hash
from app.evaluation import EvaluationService, compare_records


class EvalGraderVersionTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_tracks_parser_version(self) -> None:
        original = grader_hash()
        self.assertEqual(original, grader_hash())
        with patch("app.eval_grader_version.sqlglot.__version__", "different-parser-version"):
            self.assertNotEqual(grader_hash(), original)
        source = (BASE_DIR / "app" / "evaluation.py").read_text(encoding="utf-8")
        changed = source.replace("return round(float(value), 4)", "return round(float(value), 3)")
        self.assertNotEqual(source, changed)
        with patch("pathlib.Path.read_text", return_value=changed):
            self.assertNotEqual(grader_hash(), original)

    def test_run_export_replay_and_comparison(self) -> None:
        service = EvaluationService()
        run = service.run("demo", ["mock.region_sales"])
        self.assertEqual(run["grader_hash"], grader_hash())
        self.assertEqual(service.export(run["id"])["run"]["grader_hash"], grader_hash())
        replay = service.get_run(run["id"], replay=True)
        self.assertEqual(replay["replay_grader_hash"], grader_hash())
        self.assertEqual(replay["grader_hash"], run["grader_hash"])
        self.assertNotIn("replay_grader_hash", service.get_run(run["id"]))

        candidate = copy.deepcopy(run)
        candidate["id"] = "candidate123"
        self.assertEqual(compare_records(run, candidate)["grader_comparison"]["status"], "same")
        candidate["grader_hash"] = "different-grader"
        with self.assertRaisesRegex(ValueError, "判分器版本不同"):
            compare_records(run, candidate)
        del candidate["grader_hash"]
        self.assertEqual(compare_records(run, candidate)["grader_comparison"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
