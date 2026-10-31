import unittest

from app.eval_latency_metrics import aggregate_sql_latencies


class LatencyMetricTests(unittest.TestCase):
    def test_nearest_rank_and_call_coverage(self) -> None:
        attempts = [
            {"sql_outcome": {"executed": index != 20}, "metrics": {"elapsed_ms": float(index)}, "stages": [{"stage": "mcp_tool_call", "tool": "query_askdata_mock", "elapsed_ms": float(index) / 2, "success": index != 20}]}
            for index in range(1, 21)
        ]
        attempts.append({"sql_outcome": {"executed": False}, "metrics": {"elapsed_ms": -1}, "stages": [{"stage": "mcp_tool_call", "tool": "query_askdata_mock", "elapsed_ms": None}]})
        attempts.append({"sql_outcome": None, "metrics": {"elapsed_ms": 999}, "stages": [{"stage": "mcp_tool_call", "tool": "query_askdata_mock", "elapsed_ms": 999}]})
        report = aggregate_sql_latencies([{"attempts": attempts}], "live")
        self.assertEqual(report["sql_attempts"], 21)
        self.assertEqual(report["end_to_end"], {"count": 20, "mean_ms": 10.5, "p95_ms": 19.0})
        self.assertEqual(report["database_tool"], {"count": 20, "mean_ms": 5.25, "p95_ms": 9.5})
        self.assertIsNone(aggregate_sql_latencies([{"attempts": attempts}], "demo"))

    def test_missing_durations_are_unknown(self) -> None:
        report = aggregate_sql_latencies([{"attempts": [{"sql_outcome": {"executed": False}, "metrics": {}, "stages": [{"stage": "mcp_tool_call", "tool": "current_datetime", "elapsed_ms": 5}]}]}], "live")
        self.assertEqual(report["end_to_end"], {"count": 0, "mean_ms": None, "p95_ms": None})
        self.assertEqual(report["database_tool"], {"count": 0, "mean_ms": None, "p95_ms": None})


if __name__ == "__main__":
    unittest.main()
