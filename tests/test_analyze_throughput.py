import csv
import statistics
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "sql" / "analyze_throughput.sql").read_text(encoding="utf-8")


def analyze(path):
    source = str(path).replace("'", "''")
    query = SQL.replace("data/raw_ticket_scans.csv", source)
    with duckdb.connect() as connection:
        result = connection.execute(query)
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


class ThroughputTests(unittest.TestCase):
    def test_real_data_matches_raw_counts_and_daily_variance(self):
        path = ROOT / "data" / "raw_ticket_scans.csv"
        with path.open(newline="", encoding="utf-8") as file:
            scans = list(csv.DictReader(file))
        intervals = analyze(path)
        raw_counts = Counter()
        breakdowns = {}
        ticket_fields = {
            "Standard": ("standard_tickets", "standard_revenue_aud"),
            "VIP": ("vip_tickets", "vip_revenue_aud"),
            "Walk-up": ("walkup_tickets", "walkup_revenue_aud"),
        }
        error_fields = {
            "Screenshot_QR": "screenshot_qr_count",
            "Wrong_Slot": "wrong_slot_count",
            "Dead_Battery": "dead_battery_count",
        }
        for row in scans:
            scan = datetime.fromisoformat(row["actual_scan_time"])
            bucket = scan.replace(minute=scan.minute // 15 * 15, second=0)
            key = (row["event_date"], bucket)
            raw_counts[key] += 1
            breakdown = breakdowns.setdefault(key, Counter())
            count_field, revenue_field = ticket_fields[row["ticket_type"]]
            breakdown[count_field] += 1
            breakdown[revenue_field] += Decimal(row["ticket_price_aud"])
            if row["issue_flag"] in error_fields:
                breakdown[error_fields[row["issue_flag"]]] += 1
        self.assertEqual(sum(row["total_arrivals"] for row in intervals), len(scans))
        self.assertEqual(
            sum(row["gross_revenue_aud"] for row in intervals),
            sum(int(row["ticket_price_aud"]) for row in scans),
        )
        for day in {row["event_date"] for row in intervals}:
            daily = [row for row in intervals if row["event_date"] == day]
            counts = [row["total_arrivals"] for row in daily]
            for row in daily:
                self.assertEqual(row["total_arrivals"], raw_counts[str(day), row["interval_15m"]])
                breakdown = breakdowns.get((str(day), row["interval_15m"]), Counter())
                for count_field, revenue_field in ticket_fields.values():
                    self.assertEqual(row[count_field], breakdown[count_field])
                    self.assertEqual(row[revenue_field], breakdown[revenue_field])
                for field in error_fields.values():
                    self.assertEqual(row[field], breakdown[field])
                self.assertAlmostEqual(row["daily_avg_arrivals"], statistics.mean(counts))
                self.assertAlmostEqual(row["daily_arrival_variance"], statistics.pvariance(counts))
                self.assertAlmostEqual(
                    row["arrival_surge_ratio"], row["total_arrivals"] / statistics.mean(counts)
                )

    def test_boundaries_empty_intervals_and_extended_hours(self):
        fields = [
            "event_date", "day_of_week", "slot_start_time", "actual_scan_time",
            "lane_id", "ticket_type", "ticket_price_aud", "issue_flag", "scan_duration_sec",
        ]
        values = [
            ("09:49:59", "Lane_1", "Standard", 38, "None", 5),
            ("09:50:00", "Lane_1", "VIP", 58, "Screenshot_QR", 6),
            ("10:10:00", "Lane_2", "Walk-up", 42, "Wrong_Slot", 60),
            ("10:10:01", "Lane_3", "Standard", 38, "None", 5),
            ("20:20:00", "Lane_4", "Standard", 38, "Dead_Battery", 5),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scans.csv"
            with path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(fields)
                for scan, lane, ticket_type, price, issue, duration in values:
                    writer.writerow([
                        "2026-11-14", "Saturday", "10:00:00",
                        f"2026-11-14 {scan}", lane, ticket_type, price, issue, duration,
                    ])
            rows = analyze(path)
        by_time = {row["interval_15m"].strftime("%H:%M"): row for row in rows}
        self.assertEqual(len(rows), 43)
        self.assertEqual(by_time["09:45"]["early_arrival_pct"], 50)
        self.assertEqual(by_time["09:45"]["ontime_arrival_pct"], 50)
        self.assertEqual(by_time["09:45"]["standard_tickets"], 1)
        self.assertEqual(by_time["09:45"]["standard_revenue_aud"], 38)
        self.assertEqual(by_time["09:45"]["vip_tickets"], 1)
        self.assertEqual(by_time["09:45"]["vip_revenue_aud"], 58)
        self.assertEqual(by_time["09:45"]["screenshot_qr_count"], 1)
        self.assertEqual(by_time["10:00"]["ontime_arrival_pct"], 50)
        self.assertEqual(by_time["10:00"]["late_arrival_pct"], 50)
        self.assertEqual(by_time["10:00"]["exception_rate_pct"], 50)
        self.assertEqual(by_time["10:00"]["active_lanes_used"], 2)
        self.assertEqual(by_time["10:00"]["walkup_tickets"], 1)
        self.assertEqual(by_time["10:00"]["walkup_revenue_aud"], 42)
        self.assertEqual(by_time["10:00"]["wrong_slot_count"], 1)
        self.assertAlmostEqual(by_time["10:00"]["estimated_lane_load_pct"], 65 / 1800 * 100)
        self.assertEqual(by_time["10:15"]["total_arrivals"], 0)
        self.assertEqual(by_time["10:15"]["gross_revenue_aud"], 0)
        for field in (
            "standard_tickets", "vip_tickets", "walkup_tickets",
            "standard_revenue_aud", "vip_revenue_aud", "walkup_revenue_aud",
            "screenshot_qr_count", "wrong_slot_count", "dead_battery_count",
        ):
            self.assertEqual(by_time["10:15"][field], 0)
        self.assertIsNone(by_time["10:15"]["avg_scan_cycle_sec"])
        self.assertIsNone(by_time["10:15"]["exception_rate_pct"])
        self.assertIsNone(by_time["10:15"]["estimated_lane_load_pct"])
        self.assertEqual(by_time["20:15"]["total_arrivals"], 1)
        self.assertEqual(by_time["20:15"]["dead_battery_count"], 1)


if __name__ == "__main__":
    unittest.main()
