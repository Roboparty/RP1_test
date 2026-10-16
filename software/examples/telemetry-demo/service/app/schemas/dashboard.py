from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.mtbf import CampaignMtbfResultView


class DashboardOverview(BaseModel):
    configured_campaign_count: int
    active_campaign_count: int
    eligible_exposure_hours: float
    relevant_failure_count: int
    pending_failure_count: int
    pending_exposure_hours: float
    last_calculated_at: datetime | None
    campaigns: list[CampaignMtbfResultView]
