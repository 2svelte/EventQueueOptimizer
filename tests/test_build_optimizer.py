import csv
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_optimizer


def interval(hour, minute, arrivals, exception_rate, revenue=0):
    return {
        "event_date": build_optimizer.SATURDAY,
        "day_of_week": "Saturday",
        "interval_15m": datetime(2026, 11, 14, hour, minute),
        "total_arrivals": arrivals,
        "gross_revenue_aud": revenue,
        "exception_rate_pct": exception_rate,
    }


class OptimizerTests(unittest.TestCase):
    def test_pipeline_exports_all_days_and_default_saturday_model(self):
        with tempfile.TemporaryDirectory() as directory:
            aggregated = Path(directory) / "aggregated.csv"
            staffing = Path(directory) / "staffing.csv"
            with patch.object(build_optimizer, "AGGREGATED", aggregated), patch.object(
                build_optimizer, "STAFFING", staffing
            ):
                build_optimizer.main([])
            with aggregated.open(newline="", encoding="utf-8") as file:
                aggregated_rows = list(csv.DictReader(file))
            with staffing.open(newline="", encoding="utf-8") as file:
                staffing_rows = list(csv.DictReader(file))
        self.assertEqual(sum(int(row["total_arrivals"]) for row in aggregated_rows), 10000)
        self.assertEqual(sum(int(row["total_arrivals"]) for row in staffing_rows), 3800)
        self.assertTrue(all(row["event_date"] == "2026-11-14" for row in staffing_rows))
        self.assertEqual(len(staffing_rows), sum(
            row["event_date"] == "2026-11-14" for row in aggregated_rows
        ))
        self.assertAlmostEqual(sum(float(row["static_labor_cost_aud"]) for row in staffing_rows), 1048.125)
        self.assertAlmostEqual(sum(float(row["dynamic_labor_cost_aud"]) for row in staffing_rows), 471.25)
        self.assertAlmostEqual(max(float(row["dynamic_max_wait_min"]) for row in staffing_rows), 4.3)
        self.assertTrue(all(float(row["dynamic_max_wait_min"]) <= 5 for row in staffing_rows))

    def test_backlog_carryover_minimum_lanes_and_empty_interval(self):
        rows = [
            interval(10, 0, 100, 0, 3800),
            interval(10, 15, 1, 100, 38),
            interval(10, 30, 0, None),
        ]
        result = build_optimizer.staffing_rows(rows, 32, 5, 20, 100, 1)
        first, second, empty = result
        self.assertEqual(first["recommended_lanes"], 2)
        self.assertEqual(first["dynamic_end_queue"], 10)
        self.assertEqual(second["dynamic_start_queue"], 10)
        self.assertEqual(second["recommended_lanes"], 4)
        self.assertEqual(second["dynamic_max_wait_min"], 5)
        self.assertEqual(second["dynamic_end_queue"], 0)
        self.assertEqual(second["static_start_queue"], first["static_end_queue"])
        self.assertEqual(second["static_end_queue"], 48.5)
        self.assertEqual(empty["recommended_lanes"], 0)
        self.assertEqual(empty["dynamic_max_wait_min"], 0)
        self.assertIsNone(empty["dynamic_labor_revenue_ratio"])

    def test_invalid_inputs_are_rejected(self):
        for args in (
            ["--target-wait", "0"],
            ["--base-cycle", "nan"],
            ["--hourly-rate", "-1"],
            ["--exception-penalty", "-1"],
            ["--baseline-lanes", "1.5"],
        ):
            with self.subTest(args=args), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    build_optimizer.main(args)
                self.assertEqual(error.exception.code, 2)

    def test_scenario_options_change_exported_estimates(self):
        with tempfile.TemporaryDirectory() as directory:
            aggregated = Path(directory) / "aggregated.csv"
            staffing = Path(directory) / "staffing.csv"
            with patch.object(build_optimizer, "AGGREGATED", aggregated), patch.object(
                build_optimizer, "STAFFING", staffing
            ):
                build_optimizer.main([
                    "--target-wait", "2", "--base-cycle", "20",
                    "--exception-penalty", "120", "--baseline-lanes", "1",
                ])
            with staffing.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
        self.assertGreater(max(int(row["recommended_lanes"]) for row in rows), 4)
        self.assertTrue(all(float(row["dynamic_max_wait_min"]) <= 2 + 1e-9 for row in rows))
        self.assertGreater(max(float(row["static_max_wait_min"]) for row in rows), 2)

    def test_missing_exception_rate_is_not_hidden_for_nonempty_interval(self):
        with self.assertRaisesRegex(ValueError, "Missing exception rate"):
            build_optimizer.staffing_rows([interval(10, 0, 1, None)], 32, 5, 6, 60, 3)


if __name__ == "__main__":
    unittest.main()
