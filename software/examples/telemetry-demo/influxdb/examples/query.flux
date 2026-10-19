// Replace bucket/run_id with the current local configuration.
from(bucket: "telemetry")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "joint_telemetry")
  |> filter(fn: (r) => r.run_id == "demo-synthetic")
  |> keep(columns: ["_time", "_field", "_value", "run_id", "case_id", "subject", "source_kind"])
  |> sort(columns: ["_time"])
