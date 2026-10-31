import unittest

from app.eval_sql_metrics import aggregate_sql_attempts, score_sql_attempt


class SqlMetricTests(unittest.TestCase):
    def test_execution_and_result_use_real_database_tool(self) -> None:
        task = {"route": "database_query", "database": "askdata_mock"}
        grade = {"checks": {"result": True}}
        success = {"tool_calls": [{"database": "askdata_mock", "sql": "SELECT 1", "success": True}]}
        self.assertEqual(score_sql_attempt(task, success, grade, "live"), {"executed": True, "result_correct": True})
        self.assertEqual(score_sql_attempt(task, success, {"checks": {"result": False}}, "live"), {"executed": True, "result_correct": False})
        for result in ({}, {"sql": "SELECT 1", "status": "completed"}, {"tool_calls": [{"database": "other", "sql": "SELECT 1", "success": True}]}, {"tool_calls": [{"database": "askdata_mock", "sql": "SELECT 1", "success": False}]}):
            self.assertEqual(score_sql_attempt(task, result, grade, "live"), {"executed": False, "result_correct": False})
        clarification = {**task, "kind": "clarification"}
        self.assertTrue(score_sql_attempt(clarification, {"initial": {}, "resumed": success}, grade, "live")["executed"])
        self.assertIsNone(score_sql_attempt(task, success, grade, "demo"))
        self.assertIsNone(score_sql_attempt({"route": "direct_response"}, success, grade, "live"))
        self.assertIsNone(score_sql_attempt({**task, "kind": "security_probe"}, success, grade, "live"))

    def test_aggregate_counts_attempts(self) -> None:
        self.assertEqual(aggregate_sql_attempts([{"executed": True, "result_correct": True}, {"executed": False, "result_correct": False}, None]), {"attempts": 2, "executed": 1, "result_correct": 1})


if __name__ == "__main__":
    unittest.main()
