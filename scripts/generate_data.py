"""Generate reproducible synthetic ticket scan logs for the case study."""

import csv
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "raw_ticket_scans.csv"
SEED = 20261113
DAY_COUNTS = (
    (date(2026, 11, 13), 3000),
    (date(2026, 11, 14), 3800),
    (date(2026, 11, 15), 3200),
)
FIELDS = (
    "ticket_id",
    "event_date",
    "day_of_week",
    "slot_start_time",
    "actual_scan_time",
    "lane_id",
    "ticket_type",
    "ticket_price_aud",
    "issue_flag",
    "scan_duration_sec",
)


def slot_weights(day: date) -> list[int]:
    """Weight demand by day and half-hour slot."""
    weights = []
    for slot in range(20):
        hour = 10 + slot // 2
        if day.weekday() == 4:
            weights.append(1 if hour < 12 else 3 if hour < 17 else 5)
        elif day.weekday() == 5:
            weights.append(5 if 12 <= hour < 17 else 3)
        else:
            weights.append(3)
    return weights


def generate_rows() -> list[dict[str, str | int]]:
    rng = random.Random(SEED)
    total = sum(count for _, count in DAY_COUNTS)
    offsets = [(-600, 600)] * (total * 60 // 100)
    offsets += [(-1500, -660)] * (total * 25 // 100)
    offsets += [(660, 2100)] * (total * 15 // 100)
    issues = (
        ["None"] * (total * 91 // 100)
        + ["Screenshot_QR"] * (total * 4 // 100)
        + ["Wrong_Slot"] * (total * 3 // 100)
        + ["Dead_Battery"] * (total * 2 // 100)
    )
    rng.shuffle(offsets)
    rng.shuffle(issues)
    pending = []
    ticket_number = 100001

    for day, count in DAY_COUNTS:
        weights = slot_weights(day)
        for _ in range(count):
            slot_index = rng.choices(range(20), weights=weights, k=1)[0]
            slot = datetime.combine(day, time(10)) + timedelta(minutes=30 * slot_index)
            lower, upper = offsets[ticket_number - 100001]
            issue = issues[ticket_number - 100001]
            duration = rng.randint(4, 7) if issue == "None" else rng.randint(45, 90)
            ticket_type = rng.choices(
                ["Standard", "VIP", "Walk-up"], weights=[75, 15, 10], k=1
            )[0]
            price = {"Standard": 38, "VIP": 58, "Walk-up": 42}[ticket_type]

            # Leave room for short waits if all four simulated lanes are busy.
            requested_scan = slot + timedelta(seconds=rng.randint(lower, upper - 60))
            pending.append(
                (
                    requested_scan,
                    slot + timedelta(seconds=upper),
                    day,
                    slot,
                    ticket_number,
                    ticket_type,
                    price,
                    issue,
                    duration,
                )
            )
            ticket_number += 1

    lane_available = {f"Lane_{number}": datetime.min for number in range(1, 5)}
    rows: list[dict[str, str | int]] = []
    for requested, latest, day, slot, number, ticket_type, price, issue, duration in sorted(
        pending
    ):
        free_lanes = [
            lane for lane, available in lane_available.items() if available <= requested
        ]
        lane = (
            rng.choice(free_lanes)
            if free_lanes
            else min(lane_available, key=lambda name: lane_available[name])
        )
        scan = max(requested, lane_available[lane])
        if scan > latest:
            raise RuntimeError(f"Lane congestion moved ticket {number} outside its arrival window")
        lane_available[lane] = scan + timedelta(seconds=duration)
        rows.append(
            {
                "ticket_id": f"FVR-{number}",
                "event_date": day.isoformat(),
                "day_of_week": day.strftime("%A"),
                "slot_start_time": slot.strftime("%H:%M:%S"),
                "actual_scan_time": scan.strftime("%Y-%m-%d %H:%M:%S"),
                "lane_id": lane,
                "ticket_type": ticket_type,
                "ticket_price_aud": price,
                "issue_flag": issue,
                "scan_duration_sec": duration,
            }
        )

    rows.sort(key=lambda row: (row["actual_scan_time"], row["ticket_id"]))
    return rows


def main() -> None:
    rows = generate_rows()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows):,} synthetic scans to {OUTPUT}")


if __name__ == "__main__":
    main()
