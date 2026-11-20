import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import forecast_pipeline as pipeline


def schedule_rows(arrivals):
    return pd.DataFrame({
        "day_of_week": ["Friday"] * len(arrivals),
        "Time_Slot": [f"{(600 + 15 * i) // 60:02}:{(600 + 15 * i) % 60:02}"
                      for i in range(len(arrivals))],
        "minutes_since_midnight": [600 + 15 * i for i in range(len(arrivals))],
        "forecast_arrivals": arrivals,
        "forecast_exception_rate_pct": [0.0] * len(arrivals),
        "lane_cost": [8.125] * len(arrivals),
    })


class ForecastTests(unittest.TestCase):
    def test_internal_gap_boundary_and_day_edges(self):
        for quiet_slots, bridged in ((12, True), (13, False)):
            arrivals = [0, 130] + [10] * quiet_slots + [250, 0]
            result = pipeline.schedule(schedule_rows(arrivals))
            self.assertEqual(result["raw_lanes_needed"].tolist(), [1, 2] + [1] * quiet_slots + [3, 1])
            self.assertEqual(result["forecast_lanes"].tolist(),
                             [1, 2] + [3 if bridged else 1] * quiet_slots + [3, 1])
            self.assertTrue((result["forecast_labor_cost_aud"] == result["forecast_lanes"] * 8.125).all())

    def test_capacity_limit_and_queue_carry_over(self):
        end, wait = pipeline.queue_interval(200, 0, 1, 0)
        self.assertEqual(end, 50)
        self.assertEqual(wait, 5)
        end, wait = pipeline.queue_interval(0, 0, 1, end)
        self.assertEqual(end, 0)
        self.assertEqual(wait, 5)
        with self.assertRaisesRegex(ValueError, "No feasible schedule"):
            pipeline.schedule(schedule_rows([601]))
        self.assertEqual(pipeline.schedule(schedule_rows([600])).iloc[0]["raw_lanes_needed"], 5)

    def test_end_to_end_and_ticket_multiplier(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "forecast.csv"
            with contextlib.redirect_stdout(io.StringIO()) as summary:
                pipeline.main(["--output", str(output)])
            result = pd.read_csv(output)
            self.assertEqual(list(result.columns), list(pipeline.OUTPUT_COLUMNS))
            self.assertEqual(len(result), 129)
            self.assertEqual(set(result["day_of_week"]), {"Friday", "Saturday", "Sunday"})
            self.assertIn("lane_hours", summary.getvalue())
            self.assertTrue((result["forecast_max_wait_min"] < 5).all())
            capacity = 900 / (6 + result["forecast_exception_rate_pct"] / 100 * 30)
            self.assertTrue((result["forecast_arrivals"] <= 0.8 * result["forecast_lanes"] * capacity).all())
            self.assertTrue(np.allclose(result["forecast_labor_cost_aud"], result["forecast_lanes"] * 8.125))
            source = pipeline.load_data(ROOT / "data" / "staffing_15min.csv")
            scaled = pipeline.train_forecasts(source, 1.1)
            expected = np.ceil(pipeline.train_forecasts(source, 1.0)["forecast_arrivals"] * 1.1)
            # Scaling is applied before integer rounding.
            self.assertTrue((scaled["forecast_arrivals"] <= expected).all())
            self.assertTrue((scaled["forecast_arrivals"] >= result["forecast_arrivals"]).all())

    def test_zero_lane_cost_and_missing_rate(self):
        source = pd.read_csv(ROOT / "data" / "staffing_15min.csv").iloc[:2].copy()
        source.loc[source.index[0], ["total_arrivals", "recommended_lanes", "dynamic_labor_cost_aud"]] = 0
        source.loc[source.index[0], "exception_rate_pct"] = np.nan
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            source.to_csv(path, index=False)
            result = pipeline.load_data(path)
            self.assertEqual(result.iloc[0]["lane_cost"], 8.125)
            self.assertEqual(result.iloc[0]["exception_rate_pct"], 0)
            source.loc[source.index[0], "total_arrivals"] = 1
            source.to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, "finite"):
                pipeline.load_data(path)

    def test_invalid_inputs_and_output_path(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    pipeline.main(["--ticket-multiplier", value])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.main(["--input", "same.csv", "--output", "same.csv"])
        self.assertRaises(ValueError, pipeline.parse_interval, "10:07")

    def test_multiweek_observations_do_not_get_bootstrapped(self):
        source = pd.read_csv(ROOT / "data" / "staffing_15min.csv").iloc[:3].copy()
        another_week = source.copy()
        another_week["interval_15m"] = [
            str(datetime.fromisoformat(value) + timedelta(days=7))
            for value in another_week["interval_15m"]
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weeks.csv"
            pd.concat([source, another_week], ignore_index=True).to_csv(path, index=False)
            data = pipeline.load_data(path)
        real_model = pipeline.GradientBoostingRegressor
        sizes = []

        class RecordingModel:
            def __init__(self, **kwargs):
                self.model = real_model(**kwargs)

            def fit(self, features, target):
                sizes.append(len(features))
                return self.model.fit(features, target)

            def predict(self, features):
                return self.model.predict(features)

        with patch.object(pipeline, "GradientBoostingRegressor", RecordingModel):
            forecast = pipeline.train_forecasts(data, 1)
        self.assertEqual(sizes, [6, 6])
        self.assertEqual(len(forecast), 3)

    def test_single_week_gets_twelve_training_samples(self):
        data = pipeline.load_data(ROOT / "data" / "staffing_15min.csv").iloc[:3]
        real_model = pipeline.GradientBoostingRegressor
        sizes = []

        class RecordingModel:
            def __init__(self, **kwargs):
                self.model = real_model(**kwargs)

            def fit(self, features, target):
                sizes.append(len(features))
                return self.model.fit(features, target)

            def predict(self, features):
                return self.model.predict(features)

        with patch.object(pipeline, "GradientBoostingRegressor", RecordingModel):
            pipeline.train_forecasts(data, 1)
        self.assertEqual(sizes, [36, 36])


if __name__ == "__main__":
    unittest.main()
