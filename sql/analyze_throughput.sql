-- Run from the repository root in DuckDB. PostgreSQL users can import the CSV
-- into a table and replace read_csv_auto(...) with that table name.
-- Scan start time is only a proxy for arrival; queue entry is not observed.
WITH scans AS (
    SELECT
        CAST(event_date AS DATE) AS event_date,
        day_of_week,
        CAST(event_date AS DATE) + CAST(slot_start_time AS TIME) AS slot_start,
        CAST(actual_scan_time AS TIMESTAMP) AS scan_time,
        lane_id,
        CAST(ticket_price_aud AS DECIMAL(10, 2)) AS ticket_price_aud,
        issue_flag,
        CAST(scan_duration_sec AS INTEGER) AS scan_duration_sec
    FROM read_csv_auto('data/raw_ticket_scans.csv', header = true)
),
bucketed_scans AS (
    SELECT
        *,
        date_trunc('hour', scan_time)
            + floor(extract(minute FROM scan_time) / 15) * INTERVAL '15 minutes'
            AS interval_15m
    FROM scans
),
-- Include every operating interval, plus any earlier/later observed scans.
-- This prevents an idle 15-minute interval from inflating the daily average.
day_bounds AS (
    SELECT
        event_date,
        day_of_week,
        least(CAST(event_date AS TIMESTAMP) + INTERVAL '10 hours',
              min(interval_15m)) AS first_interval,
        greatest(CAST(event_date AS TIMESTAMP) + INTERVAL '19 hours 45 minutes',
                 max(interval_15m)) AS last_interval
    FROM bucketed_scans
    GROUP BY event_date, day_of_week
),
intervals AS (
    SELECT event_date, day_of_week, series.interval_15m
    FROM day_bounds
    CROSS JOIN LATERAL generate_series(
        first_interval, last_interval, INTERVAL '15 minutes'
    ) AS series(interval_15m)
),
aggregated AS (
    SELECT
        i.event_date,
        i.day_of_week,
        i.interval_15m,
        count(s.scan_time) AS total_arrivals,
        coalesce(sum(s.ticket_price_aud), 0) AS gross_revenue_aud,
        -- Percentages use 0-100 units; exactly +/-10 minutes is on time.
        100.0 * sum(CASE WHEN s.scan_time < s.slot_start - INTERVAL '10 minutes'
                         THEN 1 ELSE 0 END) / nullif(count(s.scan_time), 0)
            AS early_arrival_pct,
        100.0 * sum(CASE WHEN s.scan_time BETWEEN s.slot_start - INTERVAL '10 minutes'
                                                 AND s.slot_start + INTERVAL '10 minutes'
                         THEN 1 ELSE 0 END) / nullif(count(s.scan_time), 0)
            AS ontime_arrival_pct,
        100.0 * sum(CASE WHEN s.scan_time > s.slot_start + INTERVAL '10 minutes'
                         THEN 1 ELSE 0 END) / nullif(count(s.scan_time), 0)
            AS late_arrival_pct,
        avg(s.scan_duration_sec) AS avg_scan_cycle_sec,
        100.0 * sum(CASE WHEN s.issue_flag <> 'None' THEN 1 ELSE 0 END)
            / nullif(count(s.scan_time), 0) AS exception_rate_pct,
        count(DISTINCT s.lane_id) AS active_lanes_used,
        -- Approximate pressure only: attributes each whole scan to its start
        -- interval, and treats observed active lanes as available for 15 minutes.
        100.0 * sum(s.scan_duration_sec)
            / nullif(count(DISTINCT s.lane_id) * 900, 0) AS estimated_lane_load_pct
    FROM intervals AS i
    LEFT JOIN bucketed_scans AS s
        ON i.event_date = s.event_date
        AND i.day_of_week = s.day_of_week
        AND i.interval_15m = s.interval_15m
    GROUP BY i.event_date, i.day_of_week, i.interval_15m
),
daily_stats AS (
    SELECT
        *,
        avg(total_arrivals) OVER (PARTITION BY event_date) AS daily_avg_arrivals,
        var_pop(total_arrivals) OVER (PARTITION BY event_date) AS daily_arrival_variance
    FROM aggregated
)
SELECT
    event_date,
    day_of_week,
    interval_15m,
    total_arrivals,
    gross_revenue_aud,
    early_arrival_pct,
    ontime_arrival_pct,
    late_arrival_pct,
    avg_scan_cycle_sec,
    exception_rate_pct,
    active_lanes_used,
    total_arrivals / nullif(daily_avg_arrivals, 0) AS arrival_surge_ratio,
    daily_avg_arrivals,
    daily_arrival_variance,
    estimated_lane_load_pct
FROM daily_stats
ORDER BY event_date, interval_15m;
