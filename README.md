# Fever Melbourne - Onsite Operations Optimizer

This project models check-in demand for a fictional Melbourne exhibition. It is
a synthetic case study, not a record of Fever's actual operations.

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

Install the SQL dependency with `python -m pip install -r requirements.txt`.
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

The interactive staffing workbook is planned for a later milestone.

After installing the dependency, run the generator and SQL checks with:

```shell
python -m unittest discover -s tests
```
