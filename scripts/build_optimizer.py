"""Export DuckDB interval analysis and modeled staffing data for Power BI."""

import argparse
import csv
import math
from datetime import date
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw_ticket_scans.csv"
SQL_PATH = ROOT / "sql" / "analyze_throughput.sql"
AGGREGATED = ROOT / "data" / "aggregated_15min.csv"
STAFFING = ROOT / "data" / "staffing_15min.csv"
SATURDAY = date(2026, 11, 14)

STAFFING_FIELDS = (
    "event_date",
    "day_of_week",
    "interval_15m",
    "total_arrivals",
    "gross_revenue_aud",
    "exception_rate_pct",
    "hourly_rate_aud",
    "target_wait_min",
    "base_scan_cycle_sec",
    "exception_penalty_sec",
    "effective_scan_cycle_sec",
    "static_lanes",
    "static_capacity",
    "static_start_queue",
    "static_end_queue",
    "static_max_wait_min",
    "recommended_lanes",
    "dynamic_capacity",
    "dynamic_start_queue",
    "dynamic_end_queue",
    "dynamic_max_wait_min",
    "static_labor_cost_aud",
    "dynamic_labor_cost_aud",
    "static_labor_revenue_ratio",
    "dynamic_labor_revenue_ratio",
)


def positive_float(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return value


def nonnegative_float(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than or equal to zero")
    return value


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive whole number")
    return value


def load_aggregates():
    sql = SQL_PATH.read_text(encoding="utf-8")
    source = "data/raw_ticket_scans.csv"
    if source not in sql:
        raise ValueError(f"Expected CSV source {source!r} in {SQL_PATH}")
    query = sql.replace(source, str(RAW).replace("'", "''"))
    with duckdb.connect() as connection:
        result = connection.execute(query)
        columns = [column[0] for column in result.description]
        rows = [dict(zip(columns, row)) for row in result.fetchall()]
    if not rows:
        raise ValueError("The SQL query returned no intervals")
    return columns, rows


def staffing_rows(rows, hourly_rate, target_wait, base_cycle, exception_penalty, baseline_lanes):
    saturday = sorted(
        (row for row in rows if row["event_date"] == SATURDAY),
        key=lambda row: row["interval_15m"],
    )
    if not saturday:
        raise ValueError(f"No intervals found for Saturday {SATURDAY}")

    static_queue = 0.0
    dynamic_queue = 0.0
    result = []
    for row in saturday:
        arrivals = row["total_arrivals"]
        revenue = row["gross_revenue_aud"]
        exception_rate = row["exception_rate_pct"]
        if exception_rate is None and arrivals:
            raise ValueError(f"Missing exception rate for {row['interval_15m']}")
        rate = exception_rate if exception_rate is not None else 0
        cycle = base_cycle + rate / 100 * exception_penalty

        static_capacity = baseline_lanes * 900 / cycle
        static_end = max(0.0, static_queue + arrivals - static_capacity)
        static_wait = max(static_queue, static_end) * 15 / static_capacity

        if dynamic_queue + arrivals:
            recommended = max(
                1,
                math.ceil(dynamic_queue * cycle / (60 * target_wait)),
                math.ceil((dynamic_queue + arrivals) * cycle / (60 * (15 + target_wait))),
            )
        else:
            recommended = 0
        dynamic_capacity = recommended * 900 / cycle
        dynamic_end = max(0.0, dynamic_queue + arrivals - dynamic_capacity)
        dynamic_wait = (
            max(dynamic_queue, dynamic_end) * 15 / dynamic_capacity
            if dynamic_capacity else 0.0
        )
        static_cost = baseline_lanes * hourly_rate / 4
        dynamic_cost = recommended * hourly_rate / 4

        result.append({
            "event_date": row["event_date"],
            "day_of_week": row["day_of_week"],
            "interval_15m": row["interval_15m"],
            "total_arrivals": arrivals,
            "gross_revenue_aud": revenue,
            "exception_rate_pct": exception_rate,
            "hourly_rate_aud": hourly_rate,
            "target_wait_min": target_wait,
            "base_scan_cycle_sec": base_cycle,
            "exception_penalty_sec": exception_penalty,
            "effective_scan_cycle_sec": cycle,
            "static_lanes": baseline_lanes,
            "static_capacity": static_capacity,
            "static_start_queue": static_queue,
            "static_end_queue": static_end,
            "static_max_wait_min": static_wait,
            "recommended_lanes": recommended,
            "dynamic_capacity": dynamic_capacity,
            "dynamic_start_queue": dynamic_queue,
            "dynamic_end_queue": dynamic_end,
            "dynamic_max_wait_min": dynamic_wait,
            "static_labor_cost_aud": static_cost,
            "dynamic_labor_cost_aud": dynamic_cost,
            "static_labor_revenue_ratio": static_cost / float(revenue) if revenue else None,
            "dynamic_labor_revenue_ratio": dynamic_cost / float(revenue) if revenue else None,
        })
        static_queue, dynamic_queue = static_end, dynamic_end
    return result


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hourly-rate", type=positive_float, default=32.5,
        help="staff cost per hour in AUD (default: 32.50)",
    )
    parser.add_argument(
        "--target-wait", type=positive_float, default=5.0,
        help="modeled maximum wait in minutes (default: 5)",
    )
    parser.add_argument(
        "--base-cycle", type=positive_float, default=6.0,
        help="base scan cycle in seconds (default: 6)",
    )
    parser.add_argument(
        "--exception-penalty", type=nonnegative_float, default=60.0,
        help="additional seconds per exception (default: 60)",
    )
    parser.add_argument(
        "--baseline-lanes", type=positive_int, default=3,
        help="flat staffing baseline in lanes (default: 3)",
    )
    args = parser.parse_args(argv)

    columns, rows = load_aggregates()
    staffing = staffing_rows(
        rows, args.hourly_rate, args.target_wait, args.base_cycle,
        args.exception_penalty, args.baseline_lanes,
    )
    write_csv(AGGREGATED, columns, rows)
    write_csv(STAFFING, STAFFING_FIELDS, staffing)
    print(f"Wrote {len(rows)} intervals to {AGGREGATED}")
    print(f"Wrote {len(staffing)} Saturday staffing estimates to {STAFFING}")


if __name__ == "__main__":
    main()
