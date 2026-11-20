"""Forecast 15-minute demand and build a bounded, smoothed lane schedule."""

import argparse
import math
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor


ROOT = Path(__file__).resolve().parents[1]
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
REQUIRED = (
    "day_of_week", "interval_15m", "total_arrivals", "exception_rate_pct",
    "dynamic_capacity", "recommended_lanes", "dynamic_max_wait_min",
    "dynamic_labor_cost_aud",
)
FEATURES = (
    "hour", "minute", "minutes_since_midnight", "sin_time", "cos_time",
    *(f"day_{day}" for day in DAYS),
)
OUTPUT_COLUMNS = (
    "day_of_week", "Time_Slot", "forecast_arrivals", "forecast_exception_rate_pct",
    "raw_lanes_needed", "forecast_lanes", "forecast_max_wait_min",
    "forecast_labor_cost_aud",
)


def positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return value


def parse_interval(value: object) -> tuple[time, datetime | None]:
    text = str(value).strip()
    try:
        if len(text) == 5 and text[2] == ":":
            parsed_time = time.fromisoformat(text)
            stamp = None
        else:
            stamp = datetime.fromisoformat(text)
            parsed_time = stamp.time()
    except ValueError as exc:
        raise ValueError(f"Invalid interval_15m: {text!r}; expected HH:MM or ISO timestamp") from exc
    if parsed_time.minute % 15 or parsed_time.second or parsed_time.microsecond:
        raise ValueError(f"interval_15m is not aligned to a 15-minute boundary: {text!r}")
    return parsed_time, stamp


def load_data(path: Path) -> pd.DataFrame:
    """Validate observations and derive a per-lane cost for each row."""
    data = pd.read_csv(path)
    missing = set(REQUIRED) - set(data.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")
    if data.empty:
        raise ValueError("Input contains no observations")
    if not data["day_of_week"].isin(DAYS).all():
        raise ValueError("day_of_week must contain full English weekday names")

    intervals = [parse_interval(value) for value in data["interval_15m"]]
    for day, (_, stamp) in zip(data["day_of_week"], intervals):
        if stamp is not None and DAYS[stamp.weekday()] != day:
            raise ValueError(f"Timestamp {stamp} does not match day_of_week {day}")
    stamps = [stamp for _, stamp in intervals if stamp is not None]
    if len(stamps) != len(set(stamps)):
        raise ValueError("Duplicate timestamp observations are not supported")
    data["Time_Slot"] = [slot.strftime("%H:%M") for slot, _ in intervals]
    data["minutes_since_midnight"] = [slot.hour * 60 + slot.minute for slot, _ in intervals]
    data["hour"] = data["minutes_since_midnight"] // 60
    data["minute"] = data["minutes_since_midnight"] % 60
    angle = 2 * np.pi * data["minutes_since_midnight"] / 1440
    data["sin_time"], data["cos_time"] = np.sin(angle), np.cos(angle)
    for day in DAYS:
        data[f"day_{day}"] = (data["day_of_week"] == day).astype(int)

    numeric = list(REQUIRED[2:])
    for column in numeric:
        data[column] = pd.to_numeric(data[column], errors="raise")
    # The source optimizer leaves the exception rate blank for empty intervals.
    empty_rate = data["exception_rate_pct"].isna() & (data["total_arrivals"] == 0)
    data.loc[empty_rate, "exception_rate_pct"] = 0.0
    if not np.isfinite(data[numeric].to_numpy(dtype=float)).all():
        raise ValueError("Required numeric values must be finite; exception rate may be blank only for zero arrivals")
    if (data[numeric] < 0).any().any():
        raise ValueError("Required numeric values must be nonnegative")
    if (data["exception_rate_pct"] > 100).any():
        raise ValueError("exception_rate_pct must be between 0 and 100")
    for column in ("total_arrivals", "recommended_lanes"):
        if (data[column] % 1 != 0).any():
            raise ValueError(f"{column} must contain whole numbers")

    lanes = data["recommended_lanes"]
    costs = data["dynamic_labor_cost_aud"]
    if ((lanes == 0) & (costs != 0)).any():
        raise ValueError("Zero historical lanes cannot have a nonzero labor cost")
    data["lane_cost"] = costs.div(lanes.where(lanes > 0))
    if data["lane_cost"].isna().any() and "hourly_rate_aud" in data.columns:
        hourly = pd.to_numeric(data["hourly_rate_aud"], errors="raise")
        if not np.isfinite(hourly.to_numpy(dtype=float)).all() or (hourly < 0).any():
            raise ValueError("hourly_rate_aud must contain finite nonnegative values")
        data["lane_cost"] = data["lane_cost"].fillna(hourly / 4)
    if data["lane_cost"].isna().any():
        known = data["lane_cost"].dropna().to_numpy()
        if not len(known) or not np.allclose(known, known[0], rtol=1e-9, atol=1e-9):
            raise ValueError("Cannot infer single-lane cost for zero-lane intervals")
        data["lane_cost"] = data["lane_cost"].fillna(known[0])

    for day, group in data.groupby("day_of_week", sort=False):
        slots = np.sort(group["minutes_since_midnight"].unique())
        if len(slots) > 1 and not np.all(np.diff(slots) == 15):
            raise ValueError(f"Missing 15-minute intervals for {day}; queue carry-over would be ambiguous")
    return data


def train_forecasts(data: pd.DataFrame, ticket_multiplier: float) -> pd.DataFrame:
    """Train 85th-quantile models; bootstrap only a wholly single-week input."""
    keys = ["day_of_week", "Time_Slot"]
    if data.groupby(keys).size().max() == 1:
        rng = np.random.default_rng(42)
        training = pd.concat([data.copy() for _ in range(12)], ignore_index=True)
        training["total_arrivals"] = np.maximum(
            0, np.rint(rng.normal(training["total_arrivals"], training["total_arrivals"] * 0.12))
        )
        jitter = np.clip(rng.normal(0, 1.5, len(training)), -3, 3)
        training["exception_rate_pct"] = np.clip(training["exception_rate_pct"] + jitter, 0, 100)
    else:
        training = data

    slots = data.groupby(keys, sort=False, as_index=False).first()
    slots["lane_cost"] = data.groupby(keys, sort=False)["lane_cost"].mean().to_numpy()
    for target, output in (
        ("total_arrivals", "forecast_arrivals"),
        ("exception_rate_pct", "forecast_exception_rate_pct"),
    ):
        model = GradientBoostingRegressor(loss="quantile", alpha=0.85, random_state=42)
        model.fit(training[list(FEATURES)], training[target])
        slots[output] = model.predict(slots[list(FEATURES)])
    arrivals = np.ceil(np.maximum(0, slots["forecast_arrivals"]) * ticket_multiplier)
    if not np.isfinite(arrivals).all():
        raise ValueError("Ticket multiplier produces non-finite forecast arrivals")
    slots["forecast_arrivals"] = arrivals
    slots["forecast_exception_rate_pct"] = slots["forecast_exception_rate_pct"].clip(0, 100)
    slots["_day_order"] = slots["day_of_week"].map({day: i for i, day in enumerate(DAYS)})
    return slots.sort_values(["_day_order", "minutes_since_midnight"]).reset_index(drop=True)


def queue_interval(arrivals: float, exception_rate: float, lanes: int, backlog: float) -> tuple[float, float]:
    """Return end backlog and maximum fluid-queue wait in minutes."""
    capacity = lanes * 900 / (6 + exception_rate / 100 * 30)
    end = max(0.0, backlog + arrivals - capacity)
    return end, max(backlog, end) * 15 / capacity


def schedule(forecast: pd.DataFrame) -> pd.DataFrame:
    """Choose feasible lanes, bridge internal gaps, then resimulate the roster."""
    result = forecast.copy()
    raw = np.ones(len(result), dtype=int)
    smoothed = np.ones(len(result), dtype=int)
    waits = np.zeros(len(result))
    for day, group in result.groupby("day_of_week", sort=False):
        indices = group.index.to_numpy()
        backlog = 0.0
        for index in indices:
            arrivals = result.at[index, "forecast_arrivals"]
            rate = result.at[index, "forecast_exception_rate_pct"]
            single_capacity = 900 / (6 + rate / 100 * 30)
            for lanes in range(1, 6):
                end, wait = queue_interval(arrivals, rate, lanes, backlog)
                if arrivals <= 0.8 * lanes * single_capacity and wait < 5.0:
                    raw[index] = lanes
                    backlog = end
                    break
            else:
                raise ValueError(
                    f"No feasible schedule with up to 5 lanes for {day} "
                    f"{result.at[index, 'Time_Slot']}; reduce demand or add capacity"
                )
        smoothed[indices] = raw[indices]
        peaks = [i for i in indices if raw[i] > 1]
        for left, right in zip(peaks, peaks[1:]):
            gap = result.at[right, "minutes_since_midnight"] - result.at[left, "minutes_since_midnight"] - 15
            if 0 < gap <= 180:
                smoothed[left + 1:right] = max(raw[left], raw[right])
        backlog = 0.0
        for index in indices:
            backlog, waits[index] = queue_interval(
                result.at[index, "forecast_arrivals"],
                result.at[index, "forecast_exception_rate_pct"],
                int(smoothed[index]), backlog,
            )
            if waits[index] >= 5.0:
                raise ValueError(f"Smoothed roster exceeds SLA for {day} {result.at[index, 'Time_Slot']}")
    result["raw_lanes_needed"] = raw
    result["forecast_lanes"] = smoothed
    result["forecast_max_wait_min"] = waits
    result["forecast_labor_cost_aud"] = result["lane_cost"] * smoothed
    result["forecast_arrivals"] = result["forecast_arrivals"].astype(int)
    return result[list(OUTPUT_COLUMNS)]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "staffing_15min.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "forecast_15min.csv")
    parser.add_argument("--ticket-multiplier", type=positive_float, default=1.0)
    args = parser.parse_args(argv)
    if args.input.resolve() == args.output.resolve():
        parser.error("--input and --output must be different files")
    try:
        forecast = schedule(train_forecasts(load_data(args.input), args.ticket_multiplier))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        forecast.to_csv(args.output, index=False)
    except (ValueError, OSError, pd.errors.ParserError) as exc:
        parser.exit(1, f"Forecast failed: {exc}\n")
    summary = forecast.groupby("day_of_week", sort=False).agg(
        arrivals=("forecast_arrivals", "sum"),
        peak_wait_min=("forecast_max_wait_min", "max"),
        lane_intervals=("forecast_lanes", "sum"),
        labor_aud=("forecast_labor_cost_aud", "sum"),
    )
    summary["lane_hours"] = summary.pop("lane_intervals") / 4
    summary = summary[["arrivals", "peak_wait_min", "lane_hours", "labor_aud"]]
    print(summary.to_string(float_format=lambda value: f"{value:.2f}"))
    print(f"\nWrote {len(forecast)} intervals to {args.output}")


if __name__ == "__main__":
    main()
