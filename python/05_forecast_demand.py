from pathlib import Path
from collections import defaultdict
from datetime import date
import csv
import json

root = Path(__file__).resolve().parents[1]
source = root / "data" / "raw" / "customer_demand.csv"
output = root / "data" / "processed" / "demand_forecast.csv"
report = root / "reports" / "05_forecast_metrics.json"

train_end = date(2026, 2, 14)  # Days 1–45
history = defaultdict(list)
test_rows = []

with source.open(newline="", encoding="utf-8") as file:
    for row in csv.DictReader(file):
        day = date.fromisoformat(row["demand_date"])
        key = (row["store_id"], row["sku"])
        actual = int(row["requested_units"])

        if day <= train_end:
            history[key].append(actual)
        else:
            test_rows.append((day, key, actual))

# Forecast each store–medicine pair from its most recent 14 training days.
daily_forecast = {
    key: sum(values[-14:]) / len(values[-14:])
    for key, values in history.items()
}

results = []
absolute_error = 0
signed_error = 0
actual_total = 0

for day, key, actual in test_rows:
    predicted = daily_forecast[key]
    absolute_error += abs(actual - predicted)
    signed_error += predicted - actual
    actual_total += actual

    results.append({
        "forecast_date": day.isoformat(),
        "store_id": key[0],
        "sku": key[1],
        "forecast_units": round(predicted, 2),
        "actual_requested_units": actual,
        "absolute_error_units": round(abs(actual - predicted), 2),
    })

with output.open("w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(file, fieldnames=[
        "forecast_date", "store_id", "sku", "forecast_units",
        "actual_requested_units", "absolute_error_units"
    ])
    writer.writeheader()
    writer.writerows(results)

assert len(history) == 40 * 30
assert all(len(values) == 45 for values in history.values())
assert len(results) == 40 * 30 * 15

metrics = {
    "method": "Last 14 training days' average per store and medicine",
    "training_dates": "2026-01-01 through 2026-02-14",
    "test_dates": "2026-02-15 through 2026-03-01",
    "test_forecast_records": len(results),
    "test_actual_requested_units": actual_total,
    "wape_percent": round(100 * absolute_error / actual_total, 2),
    "forecast_bias_percent": round(100 * signed_error / actual_total, 2),
    "meaning": {
        "wape": "Total absolute forecast error divided by total actual requests",
        "bias": "Positive means the forecast overestimated requests",
    },
    "validation": "Passed: test dates excluded from forecast training",
}
report.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
print(json.dumps(metrics, indent=2))
print("\nSaved forecasts in data/processed/ and evaluation in reports/.")