from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.schemas.setup import AssetCreate, CampaignCreate, ContextCreate, ProgramCreate, StationCreate


def _parts_match(case_part: str | None, asset_part: str | None) -> bool:
    if case_part == asset_part:
        return True
    return (case_part, asset_part) in {("ARM", "SARM"), ("LEG", "SLEG")}


class SetupRepository:
    """Uses the existing app role, RLS and row-change audit; never writes runtime facts."""

    def __init__(self, session: Session):
        self.session = session

    def rows(self, sql, params=None):
        return [dict(row) for row in self.session.execute(text(sql), params or {}).mappings()]

    def one(self, sql, params=None):
        rows = self.rows(sql, params)
        if not rows:
            raise ApiError(404, "not_found", "对象不存在或当前账户不可访问。")
        return rows[0]

    def reason(self, reason):
        self.session.execute(text("SELECT set_config('app.change_reason', :reason, true)"), {"reason": reason})

    def catalog(self):
        return self.rows("""
            SELECT tc.public_id AS id, tc.case_code AS code, tc.name, tc.asset_kind,
                   tc.target_part_code, p.name AS target_part_name, tc.enabled,
                   tc.domain, tc.evidence_type, v.public_id AS version_id,
                   v.version, v.procedure_spec
            FROM catalog.test_case tc
            LEFT JOIN catalog.test_target_part p ON p.part_code=tc.target_part_code
            LEFT JOIN LATERAL (
                SELECT public_id, version, procedure_spec FROM catalog.test_case_version
                WHERE test_case_id=tc.id AND status='PUBLISHED'
                ORDER BY published_at DESC NULLS LAST,id DESC LIMIT 1
            ) v ON true ORDER BY tc.asset_kind,tc.target_part_code,tc.case_code
        """)

    def options(self):
        return {
            "parts": self.rows("SELECT part_code AS code,name,asset_kind FROM catalog.test_target_part WHERE enabled ORDER BY part_code"),
            "programs": self.rows("SELECT public_id AS id,program_code AS code,name,status FROM test.test_program WHERE iam.is_system_admin() ORDER BY id DESC"),
            "stations": self.rows("SELECT s.public_id AS id,s.station_code AS code,s.name,s.station_type FROM test.station s JOIN test.lab l ON l.id=s.lab_id JOIN test.site t ON t.id=l.site_id WHERE s.enabled AND l.enabled AND t.enabled ORDER BY s.station_code"),
            "assets": self.rows("SELECT a.public_id AS id,a.asset_code AS code,a.display_name AS name,a.asset_kind,CASE WHEN a.asset_kind='WHOLE_MACHINE' THEN 'SYS' ELSE m.target_part_code END AS target_part_code FROM test.asset a LEFT JOIN test.module_profile m ON m.asset_id=a.id WHERE NOT a.voided ORDER BY a.asset_code"),
            "configurations": self.rows("SELECT c.public_id AS id,a.public_id AS asset_id,c.fingerprint,c.reliability_impact FROM test.configuration_snapshot c JOIN test.asset a ON a.id=c.asset_id WHERE NOT a.voided ORDER BY c.effective_from DESC,c.id DESC"),
        }

    def campaigns(self):
        return self.rows("""
            SELECT c.public_id AS id,c.campaign_code AS code,c.name,c.asset_kind,c.status,
                   c.planned_start,c.planned_end,p.program_code,p.name AS program_name,
                   iam.can_access_campaign(c.id,true) AS can_edit,
                   (SELECT count(*) FROM test.campaign_asset a WHERE a.campaign_id=c.id AND a.left_at IS NULL) AS asset_count,
                   (SELECT count(*) FROM test.campaign_coverage_item ci WHERE ci.campaign_id=c.id) AS case_count,
                   COALESCE((SELECT jsonb_agg(jsonb_build_object('sequence_no', s.sequence_no, 'stage_name', s.stage_name) ORDER BY s.sequence_no) FROM test.campaign_stage s WHERE s.campaign_id=c.id), '[]'::jsonb) AS stages
            FROM test.test_campaign c LEFT JOIN test.test_program p ON p.id=c.program_id ORDER BY c.id DESC
        """)

    def create_asset(self, value: AssetCreate):
        self.reason(value.reason)
        data = value.model_dump()
        asset = self.one("""INSERT INTO test.asset(asset_code,asset_kind,product_family,serial_number,display_name,notes)
            VALUES (:asset_code,:asset_kind,:product_family,:serial_number,:display_name,:notes) RETURNING id,public_id""", data)
        data["asset"] = asset["id"]
        if value.asset_kind == "MODULE":
            self.session.execute(text("INSERT INTO test.module_profile(asset_id,module_type,target_part_code) VALUES (:asset,:model,:target_part_code)"), data)
        else:
            self.session.execute(text("INSERT INTO test.whole_machine_profile(asset_id,model) VALUES (:asset,:model)"), data)
        data.update({key: json.dumps(getattr(value, key)) for key in ("hardware_manifest", "software_manifest", "parameter_manifest")})
        config = self.one("""INSERT INTO test.configuration_snapshot(asset_id,fingerprint,hardware_manifest,software_manifest,parameter_manifest,reliability_impact,captured_from,captured_by,effective_from)
            VALUES (:asset,:configuration_fingerprint,CAST(:hardware_manifest AS jsonb),CAST(:software_manifest AS jsonb),CAST(:parameter_manifest AS jsonb),'UNKNOWN','browser setup',iam.current_user_id(),now()) RETURNING public_id""", data)
        return {"id": asset["public_id"], "configuration_id": config["public_id"]}

    def create_program(self, value: ProgramCreate):
        self.reason(value.reason)
        return self.one("""INSERT INTO test.test_program(program_code,name,objective,status,owner_id)
            VALUES (:program_code,:name,:objective,'DRAFT',iam.current_user_id()) RETURNING public_id AS id""", value.model_dump())

    def create_campaign(self, value: CampaignCreate):
        self.reason(value.reason)
        params = value.model_dump()
        program = self.one("SELECT id,status FROM test.test_program WHERE public_id=:program_id FOR UPDATE", params)
        if program["status"] in {"CLOSED", "CANCELLED"}:
            raise ApiError(409, "program_closed", "所属项目已关闭，不能创建计划。")
        params["program"] = program["id"]
        return self.one("""INSERT INTO test.test_campaign(program_id,campaign_code,name,asset_kind,status,planned_start,planned_end)
            VALUES (:program,:campaign_code,:name,:asset_kind,'PLANNED',:planned_start,:planned_end) RETURNING public_id AS id""", params)

    def activate_campaign(self, campaign_id: UUID, reason: str):
        self.reason(reason)
        campaign = self.one("SELECT id,status FROM test.test_campaign WHERE public_id=:id FOR UPDATE", {"id": campaign_id})
        if campaign["status"] != "PLANNED":
            raise ApiError(409, "invalid_transition", "仅待执行计划可开放使用。")
        return self.one("UPDATE test.test_campaign SET status='ACTIVE' WHERE id=:id RETURNING public_id AS id", {"id": campaign["id"]})

    def create_station(self, value: StationCreate):
        self.reason(value.reason)
        params = value.model_dump()
        self.session.execute(text("INSERT INTO test.site(site_code,name) VALUES (:site_code,:site_name) ON CONFLICT(site_code) DO NOTHING"), params)
        site = self.one("SELECT id,name,enabled FROM test.site WHERE site_code=:site_code", params)
        if site["name"] != value.site_name or not site["enabled"]:
            raise ApiError(409, "site_conflict", "站点编号已有不同名称或已停用。")
        params["site"] = site["id"]
        self.session.execute(text("INSERT INTO test.lab(site_id,lab_code,name) VALUES (:site,:lab_code,:lab_name) ON CONFLICT(site_id,lab_code) DO NOTHING"), params)
        lab = self.one("SELECT id,name,enabled FROM test.lab WHERE site_id=:site AND lab_code=:lab_code", params)
        if lab["name"] != value.lab_name or not lab["enabled"]:
            raise ApiError(409, "lab_conflict", "实验室编号已有不同名称或已停用。")
        params["lab"] = lab["id"]
        return self.one("INSERT INTO test.station(lab_id,station_code,name,station_type) VALUES (:lab,:station_code,:name,:station_type) RETURNING public_id AS id", params)

    def contexts(self, campaign_id: UUID):
        campaign = self.one("SELECT id FROM test.test_campaign WHERE public_id=:id", {"id": campaign_id})
        return self.rows("""
            SELECT ci.public_id AS id,ci.planned_runs,ci.status,a.public_id AS asset_id,a.asset_code,
                   tc.case_code,tc.name AS case_name,v.public_id AS test_case_version_id,v.version,
                   ci.target_configuration_selector AS context
            FROM test.campaign_coverage_item ci
            JOIN test.asset a ON a.id=ci.target_asset_id
            JOIN catalog.test_case_version v ON v.id=ci.test_case_version_id
            JOIN catalog.test_case tc ON tc.id=v.test_case_id
            WHERE ci.campaign_id=:campaign ORDER BY ci.id
        """, {"campaign": campaign["id"]})

    def create_context(self, campaign_id: UUID, value: ContextCreate):
        self.reason(value.reason)
        params = value.model_dump()
        params["campaign_id"] = campaign_id
        campaign = self.one("SELECT id,asset_kind,status,iam.can_access_campaign(id,true) AS can_edit FROM test.test_campaign WHERE public_id=:campaign_id FOR UPDATE", params)
        if not campaign["can_edit"]:
            raise ApiError(403, "forbidden", "需要该计划的EDIT权限。")
        if campaign["status"] not in {"PLANNED", "ACTIVE"}:
            raise ApiError(409, "campaign_closed", "仅待执行或活动计划可配置。")
        asset = self.one("SELECT a.id,a.asset_kind,CASE WHEN a.asset_kind='WHOLE_MACHINE' THEN 'SYS' ELSE mp.target_part_code END AS part,iam.can_access_asset(a.id,true) AS can_edit FROM test.asset a LEFT JOIN test.module_profile mp ON mp.asset_id=a.id WHERE a.public_id=:asset_id AND NOT a.voided", params)
        if not asset["can_edit"]:
            raise ApiError(403, "forbidden", "需要该样品的编辑权限。")
        case = self.one("SELECT v.id,tc.asset_kind,tc.target_part_code FROM catalog.test_case_version v JOIN catalog.test_case tc ON tc.id=v.test_case_id WHERE v.public_id=:test_case_version_id AND v.status='PUBLISHED' AND tc.enabled", params)
        station = self.one("SELECT s.id,s.station_type FROM test.station s JOIN test.lab l ON l.id=s.lab_id JOIN test.site t ON t.id=l.site_id WHERE s.public_id=:station_id AND s.enabled AND l.enabled AND t.enabled", params)
        config = self.one("SELECT id,asset_id FROM test.configuration_snapshot WHERE public_id=:configuration_id", params)
        if asset["asset_kind"] != campaign["asset_kind"] or case["asset_kind"] != asset["asset_kind"] or not _parts_match(case["target_part_code"], asset["part"]) or config["asset_id"] != asset["id"] or station["station_type"] not in {"SHARED", asset["asset_kind"]}:
            raise ApiError(422, "context_mismatch", "计划、样品、用例部位、配置快照或台架类型不匹配。")
        params.update(campaign=campaign["id"], asset=asset["id"], case=case["id"], config=config["id"])
        if self.rows("SELECT id FROM test.campaign_coverage_item WHERE campaign_id=:campaign AND target_asset_id=:asset AND test_case_version_id=:case AND status<>'CANCELLED'", params):
            raise ApiError(409, "context_exists", "该计划已配置此样品和用例版本。")
        self.session.execute(text("INSERT INTO test.campaign_asset(campaign_id,asset_id) VALUES (:campaign,:asset) ON CONFLICT(campaign_id,asset_id) DO UPDATE SET left_at=NULL"), params)
        cycle = self.one("INSERT INTO test.test_cycle(cycle_code,campaign_id,asset_id,status) VALUES (:cycle_code,:campaign,:asset,'PLANNED') RETURNING id,public_id", params)
        params["cycle"] = cycle["id"]
        segment = self.one("INSERT INTO test.analysis_segment(campaign_id,cycle_id,configuration_snapshot_id,segment_no,reason,started_at) VALUES (:campaign,:cycle,:config,1,'ORIGINAL',now()) RETURNING public_id", params)
        context = {"campaign_id": str(campaign_id), "cycle_id": str(cycle["public_id"]), "segment_id": str(segment["public_id"]), "asset_id": str(value.asset_id), "configuration_id": str(value.configuration_id), "test_case_version_id": str(value.test_case_version_id), "station_id": str(value.station_id)}
        params["selector"] = json.dumps(context)
        coverage = self.one("INSERT INTO test.campaign_coverage_item(campaign_id,test_case_version_id,target_asset_id,target_configuration_selector,planned_runs) VALUES (:campaign,:case,:asset,CAST(:selector AS jsonb),:planned_runs) RETURNING public_id AS id", params)
        return {**coverage, "context": context}
