import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import RequestActor
from app.core.errors import ApiError
from app.schemas.hmi_presence import HmiHeartbeat, HmiStationList, HmiStationView


def station_view(row: dict, now: datetime) -> HmiStationView:
    fresh = row["online"] and (now - row["snapshot_at"]).total_seconds() < 20
    states = {item["state"] for item in row["runs"]}
    if not row["online"]:
        state = "offline"
    elif not fresh or "unknown" in states:
        state = "unknown"
    elif "fault" in states or row["plc"].get("phase") == "FAULT":
        state = "fault"
    elif states & {"running", "stopping"}:
        state = "running"
    elif "paused" in states:
        state = "paused"
    elif row["plc"].get("phase") == "RUNNING" and not row["plc"].get("stale", True):
        state = "powered"
    else:
        state = "idle"
    return HmiStationView(**row, snapshot_fresh=fresh, state=state)


class HmiPresenceRepository:
    def __init__(self, session: Session):
        self.session = session

    def heartbeat(self, connection_id: UUID, payload: HmiHeartbeat, actor: RequestActor) -> dict:
        values = payload.model_dump(mode="json")
        result = self.session.execute(
            text("SELECT integration.record_hmi_presence(:connection_id, :installation_id, :session_id, :station_name, :bench_id, :sequence, :snapshot_age_seconds, CAST(:runs AS jsonb), CAST(:plc AS jsonb))"),
            {**values, "connection_id": str(connection_id), "session_id": str(actor.session_public_id),
             "runs": json.dumps(values["runs"]), "plc": json.dumps(values["plc"])},
        ).scalar_one_or_none()
        if result is None:
            raise ApiError(409, "presence_session_conflict", "上位机连接已关闭或属于其他登录会话，请重新登录。")
        executions = self.session.execute(
            text("SELECT test.sync_live_runs(CAST(:connection_id AS uuid))"),
            {"connection_id": str(connection_id)},
        ).scalar_one()
        return {
            "connection_id": str(result),
            "heartbeat_interval_seconds": 10,
            "offline_after_seconds": 45,
            "executions": executions or [],
        }

    def disconnect(self, connection_id: UUID, actor: RequestActor) -> dict:
        closed = self.session.execute(
            text("SELECT integration.disconnect_hmi_presence(:connection_id, :session_id)"),
            {"connection_id": str(connection_id), "session_id": str(actor.session_public_id)},
        ).scalar_one()
        if not closed:
            raise ApiError(404, "presence_not_found", "当前登录会话没有这个上位机连接。")
        self.session.execute(
            text("SELECT test.sync_live_runs(CAST(:connection_id AS uuid))"),
            {"connection_id": str(connection_id)},
        )
        return {"connection_id": str(connection_id), "online": False}

    def list_stations(self, *, cursor: UUID | None, limit: int) -> HmiStationList:
        rows = self.session.execute(
            text("SELECT * FROM integration.list_hmi_presence(CAST(:cursor AS uuid), :limit)"),
            {"cursor": str(cursor) if cursor else None, "limit": limit + 1},
        ).mappings().all()
        now = datetime.now(timezone.utc)
        return HmiStationList(
            items=[station_view(dict(row), now) for row in rows[:limit]],
            next_cursor=rows[limit - 1]["installation_id"] if len(rows) > limit else None,
            as_of_at=now,
        )
