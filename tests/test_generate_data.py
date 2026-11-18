import csv
import sys
import unittest
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_data


class GenerateDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = generate_data.generate_rows()

    def test_distributions_and_values(self):
        self.assertEqual(len(self.rows), 10000)
        self.assertEqual(len({row["ticket_id"] for row in self.rows}), 10000)
        self.assertEqual(
            Counter(row["event_date"] for row in self.rows),
            {"2026-11-13": 3000, "2026-11-14": 3800, "2026-11-15": 3200},
        )
        self.assertEqual(
            Counter(row["issue_flag"] for row in self.rows),
            {
                "None": 9100,
                "Screenshot_QR": 400,
                "Wrong_Slot": 300,
                "Dead_Battery": 200,
            },
        )
        categories = Counter()
        demand = defaultdict(list)
        for row in self.rows:
            slot = datetime.fromisoformat(f'{row["event_date"]} {row["slot_start_time"]}')
            scan = datetime.fromisoformat(str(row["actual_scan_time"]))
            offset = (scan - slot).total_seconds()
            self.assertEqual(row["day_of_week"], slot.strftime("%A"))
            self.assertIn(slot.minute, (0, 30))
            self.assertGreaterEqual(slot.hour, 10)
            self.assertLess(slot.hour, 20)
            if -600 <= offset <= 600:
                categories["on-time"] += 1
            elif -1500 <= offset <= -660:
                categories["early"] += 1
            elif 660 <= offset <= 2100:
                categories["late"] += 1
            else:
                self.fail(f"Unexpected scan offset: {offset}")
            self.assertEqual(
                row["ticket_price_aud"],
                {"Standard": 38, "VIP": 58, "Walk-up": 42}[row["ticket_type"]],
            )
            duration = row["scan_duration_sec"]
            low, high = (4, 7) if row["issue_flag"] == "None" else (45, 90)
            self.assertTrue(low <= duration <= high)
            demand[(row["day_of_week"], slot.hour)].append(row)
        self.assertEqual(categories, {"on-time": 6000, "early": 2500, "late": 1500})
        for day in ("Friday", "Saturday", "Sunday"):
            self.assertTrue(all(demand[day, hour] for hour in range(10, 20)))
        friday_morning = sum(len(demand["Friday", hour]) for hour in range(10, 12)) / 2
        friday_evening = sum(len(demand["Friday", hour]) for hour in range(17, 20)) / 3
        saturday_afternoon = sum(len(demand["Saturday", hour]) for hour in range(12, 17)) / 5
        saturday_morning = sum(len(demand["Saturday", hour]) for hour in range(10, 12)) / 2
        self.assertGreater(friday_evening, friday_morning)
        self.assertGreater(saturday_afternoon, saturday_morning)

    def test_lanes_do_not_overlap(self):
        last_end = {}
        for row in self.rows:
            lane = row["lane_id"]
            self.assertIn(lane, {"Lane_1", "Lane_2", "Lane_3", "Lane_4"})
            scan = datetime.fromisoformat(str(row["actual_scan_time"]))
            if lane in last_end:
                self.assertGreaterEqual(scan, last_end[lane])
            last_end[lane] = scan + timedelta(seconds=row["scan_duration_sec"])

    def test_csv_matches_reproducible_generator(self):
        with (ROOT / "data" / "raw_ticket_scans.csv").open(
            newline="", encoding="utf-8"
        ) as file:
            reader = csv.DictReader(file)
            self.assertEqual(reader.fieldnames, list(generate_data.FIELDS))
            csv_rows = list(reader)
        self.assertEqual(
            csv_rows,
            [{key: str(value) for key, value in row.items()} for row in self.rows],
        )


if __name__ == "__main__":
    unittest.main()
