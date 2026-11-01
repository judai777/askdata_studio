import unittest

from pathlib import Path

from app.eval_schema_metrics import aggregate_schema_fields, load_field_targets, score_attempt_fields, score_schema_fields


class SchemaMetricTests(unittest.TestCase):
    def test_identity_deduplication_and_macro_average(self) -> None:
        def hit(database: str, table: str, field: str) -> dict:
            return {"database_id": database, "table_id": table, "field_name": field}

        first = score_schema_fields(
            ["ecommerce_ops.orders.customer_id", "ecommerce_ops.customers.customer_id"],
            [hit("ecommerce_ops", "orders", "customer_id"), hit("ecommerce_ops", "orders", "customer_id"), hit("ecommerce_ops", "other", "customer_id")],
        )
        self.assertEqual((first["target_count"], first["retrieved_count"], first["matched_count"]), (2, 2, 1))
        self.assertEqual((first["recall"], first["precision"]), (0.5, 0.5))
        self.assertEqual(first["missing"], ["ecommerce_ops.customers.customer_id"])
        second = score_schema_fields(["ecommerce_ops.orders.customer_id"], [])
        self.assertEqual((second["recall"], second["precision"]), (0.0, 0.0))
        self.assertIsNone(score_schema_fields(None, []))
        self.assertEqual(aggregate_schema_fields([first, second, None]), {"labeled_tasks": 2, "unlabeled_tasks": 1, "macro_recall": 0.25, "macro_precision": 0.25})

    def test_rejects_incomplete_labels_and_hits(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能为空"):
            score_schema_fields([], [])
        with self.assertRaisesRegex(ValueError, "database.table.field"):
            score_schema_fields(["orders.customer_id"], [])
        with self.assertRaisesRegex(ValueError, "数据库、表和字段名"):
            score_schema_fields(["ecommerce_ops.orders.customer_id"], [{"table_id": "orders", "field_name": "customer_id"}])

    def test_live_result_evidence_and_version_guard(self) -> None:
        target = ["askdata_mock.orders_current.region"]
        self.assertIsNone(score_attempt_fields(target, {}))
        self.assertIsNone(score_attempt_fields(target, {"retrieval": {}}))
        self.assertEqual(score_attempt_fields(target, {"retrieval": {"hits": []}})["recall"], 0.0)
        resumed = {"resumed": {"retrieval": {"hits": [{"database_id": "askdata_mock", "table_id": "orders_current", "field_name": "region"}]}}}
        self.assertEqual(score_attempt_fields(target, resumed)["recall"], 1.0)
        path = Path(__file__).parents[1] / "evals" / "field_targets.json"
        labels, fingerprint = load_field_targets(path, "5.3")
        self.assertEqual(len(labels), 12)
        self.assertEqual(len(fingerprint), 16)
        self.assertEqual(load_field_targets(path, "different")[0], {})


if __name__ == "__main__":
    unittest.main()
