"""Static checks for the released classic Grafana dashboard; no server needed."""

import json
from pathlib import Path


def validate(path: Path) -> None:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    assert dashboard["uid"] == "rp1-joint-telemetry"
    assert isinstance(dashboard["schemaVersion"], int)
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}
    assert set(variables) == {"DS_INFLUXDB", "run_id", "subject"}
    assert "schema.tagValues" in variables["run_id"]["query"]
    assert "${run_id:json}" in variables["subject"]["query"]
    required = {"temp_c": "celsius", "cmd_pos_deg": "suffix:°", "pos_deg": "suffix:°",
                "tracking_error_deg": "suffix:°", "spd_deg_s": "suffix:°/s", "torque_nm": "suffix:N·m"}
    found = set()
    sources = False
    ids = set()
    for panel in dashboard["panels"]:
        assert panel["id"] not in ids
        ids.add(panel["id"])
        assert all(key in panel["gridPos"] for key in ("x", "y", "w", "h"))
        for target in panel.get("targets", []):
            query = target["query"]
            assert "v.defaultBucket" in query
            assert "v.timeRangeStart" in query and "v.timeRangeStop" in query
            assert "${run_id:json}" in query and "${subject:regex}" in query
            assert target["datasource"]["uid"] == "${DS_INFLUXDB}"
            sources = sources or 'unique(column: "source_kind")' in query
            if panel["type"] == "timeseries":
                assert "createEmpty: true" in query
                assert not panel["fieldConfig"]["defaults"]["custom"]["spanNulls"]
                assert "fill(" not in query
                for field, unit in required.items():
                    if f'r._field == "{field}"' in query:
                        assert panel["fieldConfig"]["defaults"]["unit"] == unit
                        found.add(field)
    assert found == set(required) and sources
    text = path.read_text(encoding="utf-8").lower()
    assert "securejsondata" not in text and '"token"' not in text and '"password"' not in text
    print(f"Dashboard contract OK: {len(ids)} panels, {len(variables)} variables, {len(found)} fields")


if __name__ == "__main__":
    validate(Path(__file__).resolve().parents[1] / "dashboards" / "joint-telemetry.json")
