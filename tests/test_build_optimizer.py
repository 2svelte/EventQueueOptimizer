import csv
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_optimizer


def interval(hour, minute, arrivals, exception_rate, revenue=0, day=14):
    return {
        "event_date": date(2026, 11, day),
        "day_of_week": date(2026, 11, day).strftime("%A"),
        "interval_15m": datetime(2026, 11, day, hour, minute),
        "total_arrivals": arrivals,
        "gross_revenue_aud": revenue,
        "exception_rate_pct": exception_rate,
        "early_arrival_pct": None,
        "ontime_arrival_pct": None,
        "late_arrival_pct": None,
        "arrival_surge_ratio": None,
        "standard_tickets": 0,
        "vip_tickets": 0,
        "walkup_tickets": 0,
        "standard_revenue_aud": 0,
        "vip_revenue_aud": 0,
        "walkup_revenue_aud": 0,
        "screenshot_qr_count": 0,
        "wrong_slot_count": 0,
        "dead_battery_count": 0,
    }


class OptimizerTests(unittest.TestCase):
    def test_pipeline_exports_all_days_and_preserves_saturday_model(self):
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
        self.assertEqual(len(aggregated_rows), 129)
        self.assertEqual(len(staffing_rows), 129)
        self.assertEqual(
            {row["event_date"] for row in staffing_rows},
            {"2026-11-13", "2026-11-14", "2026-11-15"},
        )
        fields = (
            "event_date", "interval_15m", "early_arrival_pct",
            "ontime_arrival_pct", "late_arrival_pct", "arrival_surge_ratio",
            "standard_tickets", "vip_tickets", "walkup_tickets",
            "standard_revenue_aud", "vip_revenue_aud", "walkup_revenue_aud",
            "screenshot_qr_count", "wrong_slot_count", "dead_battery_count",
        )
        self.assertEqual(len(staffing_rows[0]), 38)
        ticket_fields = ("standard_tickets", "vip_tickets", "walkup_tickets")
        error_fields = ("screenshot_qr_count", "wrong_slot_count", "dead_battery_count")
        self.assertEqual(sum(sum(int(row[field]) for field in ticket_fields) for row in staffing_rows), 10000)
        self.assertEqual(sum(sum(int(row[field]) for field in error_fields) for row in staffing_rows), 900)
        for aggregate, staffing in zip(aggregated_rows, staffing_rows):
            for field in fields:
                self.assertEqual(staffing[field], aggregate[field])
            self.assertEqual(sum(int(staffing[field]) for field in ticket_fields), int(staffing["total_arrivals"]))
        saturday = [row for row in staffing_rows if row["event_date"] == "2026-11-14"]
        self.assertEqual(sum(int(row["total_arrivals"]) for row in saturday), 3800)
        self.assertAlmostEqual(sum(float(row["static_labor_cost_aud"]) for row in saturday), 1048.125)
        self.assertAlmostEqual(sum(float(row["dynamic_labor_cost_aud"]) for row in saturday), 471.25)
        self.assertAlmostEqual(max(float(row["dynamic_max_wait_min"]) for row in saturday), 4.3)
        self.assertTrue(all(float(row["dynamic_max_wait_min"]) <= 5 for row in staffing_rows))

    def test_backlogs_carry_within_day_but_reset_between_days(self):
        rows = [
            interval(10, 0, 0, None, day=14),
            interval(10, 15, 100, 0, day=13),
            interval(10, 0, 100, 0, day=13),
            interval(10, 0, 0, None, day=15),
        ]
        first, second, saturday, sunday = build_optimizer.staffing_rows(rows, 32, 5, 20, 0, 1)
        self.assertGreater(first["static_end_queue"], 0)
        self.assertGreater(first["dynamic_end_queue"], 0)
        self.assertEqual(second["static_start_queue"], first["static_end_queue"])
        self.assertEqual(second["dynamic_start_queue"], first["dynamic_end_queue"])
        self.assertGreater(second["static_end_queue"], 0)
        self.assertGreater(second["dynamic_end_queue"], 0)
        for row in (saturday, sunday):
            self.assertEqual(row["static_start_queue"], 0)
            self.assertEqual(row["dynamic_start_queue"], 0)
            self.assertEqual(row["recommended_lanes"], 0)

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
