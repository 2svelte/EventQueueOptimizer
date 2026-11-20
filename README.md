# Event Queue Optimizer

This project models check-in demand for a fictional Melbourne exhibition. It is
a synthetic case study, not a record of actual operations.

## Synthetic scan logs

Run the generator from the repository root with Python 3.10 or later:

```shell
python scripts/generate_data.py
```

The script uses only the Python standard library and reproducibly writes
`data/raw_ticket_scans.csv` using a fixed random seed. It generates 10,000
tickets across Friday 13 November to Sunday 15 November 2026, with 20
half-hour slots per day. Friday evening and Saturday afternoon are weighted
toward higher demand; Friday morning is quieter.

Scan times are sampled around each ticket's slot: 60% within 10 minutes,
25% 11–25 minutes early, and 15% 11–35 minutes late. Nine percent of tickets
have a slower scan exception. If all lanes are busy, a scan is deferred to the
first available lane; the underlying requested time is not included in the CSV.
The times can fall outside the 10:00–20:00 event window because guests may
scan early or late.

The generated scan timestamp is a **proxy for arrival**, not an independently
observed arrival or queue-entry timestamp. Consequently, these data alone do
not measure actual guest waiting times. Future analysis and staffing
recommendations will be modeled estimates, not validated service guarantees.

## 15-minute SQL analysis

Install the project dependencies with `python -m pip install -r requirements.txt`.
From the repository root, execute the query with DuckDB:

```shell
python -c "import duckdb; print(duckdb.sql(open('sql/analyze_throughput.sql', encoding='utf-8').read()).fetchall())"
```

The CSV reader is DuckDB-specific; after importing the CSV as a table,
PostgreSQL users can replace the `read_csv_auto(...)` source with that table.
Each day's average and population variance include empty intervals from 10:00
through 19:45 and extend to any earlier or later scan intervals. Arrival
percentages use 0–100 units, with exactly 10 minutes early or late classified
as on time. Metrics that cannot be computed for an empty interval are NULL.

The estimated lane load compares scan seconds starting in the interval with
15 minutes per observed active lane. It is an approximate bottleneck indicator,
not actual occupancy: scans can cross interval boundaries, and a lane with no
scans may still have been staffed. The query cannot infer real wait times or
guarantee the five-minute target from scan logs alone.

## Power BI pipeline

Run `python scripts/build_optimizer.py` from any working directory. It executes
the SQL and writes `data/aggregated_15min.csv` for all three days and
`data/staffing_15min.csv` with modeled staffing estimates for all three days.
It does not create an Excel workbook or a Power BI report.

The default hourly rate is AUD $32.50, target wait 5 minutes, base scan cycle
6 seconds, exception penalty 60 seconds and flat baseline 3 lanes. Use
`python scripts/build_optimizer.py --help` to see the input options. For example,
to model a 4-minute target and a $35 hourly rate:

```shell
python scripts/build_optimizer.py --target-wait 4 --hourly-rate 35
```

In Power BI Desktop, choose **Home > Get data > Text/CSV**, select
`data/staffing_15min.csv`, and load it. For a line chart, put `interval_15m`
on the X-axis and `total_arrivals`, `static_capacity`, and `dynamic_capacity`
on the Y-axis. Use `recommended_lanes` for a staffing chart. The staffing table
also includes arrival timing percentages, surge ratio, exception rate, ticket
counts and revenue by type, and counts by error type. Counts and revenue are
zero for intervals without scans.
Ensure `interval_15m` is a Date/Time column and use the column itself,
not the automatic date hierarchy, on the X-axis.

Staffing values are **fixed at export time**, not interactive Power BI
parameters. The scenario inputs are included in each staffing row. To change
a scenario, rerun the command with different options and refresh the CSV
sources in Power BI. For summary cards, use the maximum of
`static_max_wait_min` or `dynamic_max_wait_min`, and the sum of the relevant
labor cost column. For labor as a percentage of revenue, divide summed labor
cost by summed revenue; do not sum or average the per-interval ratios. The
`exception_rate_pct` column uses 0–100 units, while the labor/revenue ratios use
0–1 units. A blank ratio indicates an interval with no revenue.

## Predictive workforce forecast

Install the requirements and run the forecast from any working directory:

```shell
python src/forecast_pipeline.py
python src/forecast_pipeline.py --input data/staffing_15min.csv --output data/forecast_15min.csv --ticket-multiplier 1.10
```

Default paths are relative to the repository; explicitly supplied paths are
relative to the current working directory. The positive ticket multiplier
adjusts forecast demand (1.10 means 10% more); it does not change the
exception-rate forecast. The CSV contains one row per weekday and time slot,
including forecast arrivals and exception percentage, raw and smoothed lane
counts, modeled maximum wait, and AUD labor cost. The terminal summarizes
arrivals, peak wait, lane-hours and labor cost by weekday.

The pipeline uses weekday one-hot indicators and time-of-day features,
including sine and cosine, to train two gradient-boosted 85th-quantile
regressors. If every weekday/slot occurs only once, it creates 12 reproducible
synthetic observations per slot with normal arrival variation (12% standard
deviation) and exception-rate jitter (standard deviation 1.5 percentage
points, bounded to +/-3 and to 0–100%). Otherwise it trains on the observed
rows. Arrivals are clipped at zero, multiplied, and rounded up to whole guests.
These synthetic variations and model quantiles are assumptions, not validated
prediction intervals; historical scan timestamps are only arrival proxies.
Use multiple representative weeks and out-of-sample validation before relying
on the results.

For exception percentage `p`, one lane serves `900 / (6 + 30 * p / 100)`
guests per 15 minutes. For each day, the raw roster chooses the fewest of 1–5
lanes with forecast arrivals at most 80% of capacity and modeled wait strictly
below five minutes. In the uniform-arrival fluid model, end backlog is
`max(0, start backlog + arrivals - capacity)` and peak wait is the larger of
start and end backlog divided by the current service rate. Backlog resets at
the start of each weekday. Infeasible five-lane forecasts cause an error rather
than an SLA claim. Intervals within each weekday must be contiguous and aligned
to quarter hours; ISO timestamps must match the stated weekday. Repeated
weekday/slot observations represent different weeks; duplicate timestamps are
rejected.

With an empty starting queue, the 80% buffer and uniform-arrival assumption
normally produce zero modeled wait in every interval. This does not account
for bursts within an interval or individual guest arrival and service times.

Internal one-lane gaps of up to 180 minutes between elevated-lane intervals
are bridged at the larger neighboring lane count; gaps before the first or
after the last surge remain at one lane. The final roster is resimulated.
This is a roster-smoothing heuristic, **not** a determination of compliance
with Australian Fair Work rules, minimum shifts, breaks, or an award. Labor
cost uses historical `dynamic_labor_cost_aud / recommended_lanes` per interval,
averaged across weeks. For a zero-lane historical row, the source hourly rate
is used if present, or an unambiguous single-lane rate from other rows; an
unknown rate is rejected. Blank exception rates are accepted only for
zero-arrival rows and treated as zero.

In Power BI Desktop, choose **Home > Get data > Text/CSV**, select
`data/forecast_15min.csv`, and load it. Set `Time_Slot` to Time and use
`day_of_week` to filter or group charts. Sum forecast arrivals and labor cost;
use the maximum wait and sum lanes divided by four for lane-hours. Rerun the
pipeline and refresh Power BI after changing the input or multiplier.

The model covers all three days' 15-minute intervals, including outside
the advertised opening hours. Each day starts with an empty queue,
uses one staff member per lane, and estimates service time as the base scan
cycle plus the interval's exception rate times the exception penalty. It treats
arrivals and service as uniform within each interval. Backlog carries forward
within each day and resets at the start of the next. Estimated wait is the
larger of the start and end backlog divided by the current interval's service
rate. The dynamic lane count is the smallest integer meeting the target under
those assumptions for that interval. This is a per-interval heuristic, not a
globally optimal roster or a measured wait.

For Saturday with the default synthetic data and inputs, the model estimates
peak waits of 0.0 minutes for three flat lanes and 4.3 minutes for dynamic
staffing, with modeled labor costs of $1,048.13 and $471.25 respectively.
This is a scenario comparison, not evidence that real queues or costs will match.

Labor cost assumes staffing can change every 15 minutes and excludes paid
breaks, minimum shifts, setup, and any queue cleanup after the last interval.
The model does not schedule individual breaks or cap lane recommendations at
the four observed lanes. Check the maximum lanes and end-of-window queue in the
CSV, and arrange relief cover and any extra capacity before implementing a
roster. Changing inputs requires positive rate, target and base cycle;
nonnegative penalty; and a positive whole-number baseline lane count.

Run the checks with:

```shell
python -m unittest discover -s tests
```
