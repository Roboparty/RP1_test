import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.repositories.submissions import SubmissionRepository
from app.schemas.archives import ArchiveAnalysisCreate, ArchiveImportCreate, ArchiveCreate, ArchiveClassification, ArchiveSubmit, ArchiveVerification
from app.schemas.submissions import SubmissionCreate, TestSummary
from app.services.archive_analysis import retrospective_analysis


DETAIL = """SELECT a.id AS cursor,a.public_id AS id,a.execution_uuid,a.archive_code,
    info.owner_public_id,info.owner_name,a.owner_id=iam.current_user_id() AS is_owner,
    iam.current_principal_kind()='HUMAN' AND iam.can_write_business()
        AND (a.owner_id=iam.current_user_id() OR iam.is_system_admin())
        AND coalesce(info.submission_status,'') NOT IN ('PENDING','APPROVED') AS can_edit,
    iam.current_principal_kind()='HUMAN' AND iam.can_write_business()
        AND (a.owner_id=iam.current_user_id() OR iam.is_system_admin())
        AND a.processing_state='READY' AS can_analyze,
    a.origin,a.test_name,a.actual_object,a.station_label,a.filename,a.size_bytes,a.sha256,
    a.received_bytes,a.upload_state,a.processing_state,a.processing_error,a.revision,a.facts,a.acceptance,a.manifest,
    CASE WHEN a.classification_revision IS NULL THEN 'UNCLASSIFIED' ELSE 'CLASSIFIED' END AS classification_state,
    CASE WHEN v.revision IS NULL THEN NULL ELSE v.payload||jsonb_build_object('revision',v.revision,'created_at',v.created_at,'station_code',verified_station.station_code,'station_name',verified_station.name) END AS current_verification,
    CASE WHEN c.revision IS NULL THEN NULL ELSE c.payload||jsonb_build_object('revision',c.revision,'created_at',c.created_at) END AS classification,
    info.submission_public_id AS submission_id,info.submission_revision,info.submission_status,
    info.execution_public_id AS execution_id,info.execution_code,info.campaign_code,a.created_at,a.updated_at
    FROM integration.execution_archive a
    JOIN LATERAL integration.archive_public_info(a.id) info ON true
    LEFT JOIN integration.archive_verification v ON v.archive_id=a.id AND v.revision=a.verification_revision
    LEFT JOIN test.station verified_station ON verified_station.public_id=(v.payload->>'station_id')::uuid
    LEFT JOIN integration.archive_classification c ON c.archive_id=a.id AND c.revision=a.classification_revision
"""

ANALYSIS_DETAIL = """SELECT av.public_id AS id,av.version,av.source_test_case_version_id,av.source_case_code,av.source_case_name,
    av.source_version,av.source_kind,av.mapping_status,av.mapping_notes,av.rules_snapshot,av.acceptance,av.report,
    av.original_analysis_version,av.original_sha256,av.original_manifest_sha256,
    integration.archive_actor_name(av.archive_id,av.actor_id) AS actor_name,av.reason,av.created_at
    FROM integration.archive_analysis_version av JOIN integration.execution_archive a ON a.id=av.archive_id """


class ArchiveRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, reference: UUID | str, history=False):
        reference = str(reference)
        row = self.session.execute(text(DETAIL+"""WHERE a.public_id::text=:ref OR a.execution_uuid::text=:ref
            OR a.archive_code=:ref OR info.execution_public_id::text=:ref OR info.execution_code=:ref
            OR a.id IN (SELECT archive_id FROM integration.execution_archive_alias WHERE alias=:ref) LIMIT 1"""), {"ref": reference}).mappings().first()
        if row is None:
            raise ApiError(404, "archive_not_found", "归档不存在或当前账号无权查看。")
        item = dict(row)
        if history:
            item["verification_history"] = [dict(r) for r in self.session.execute(text("""SELECT v.revision,v.payload,v.reason,v.created_at,integration.archive_actor_name(v.archive_id,v.actor_id) AS actor_name FROM integration.archive_verification v
                JOIN integration.execution_archive a ON a.id=v.archive_id WHERE a.public_id=:id ORDER BY v.revision"""), {"id": item["id"]}).mappings()]
            item["classification_history"] = [dict(r) for r in self.session.execute(text("""SELECT c.revision,c.payload,c.reason,c.created_at,integration.archive_actor_name(c.archive_id,c.actor_id) AS actor_name FROM integration.archive_classification c
                JOIN integration.execution_archive a ON a.id=c.archive_id WHERE a.public_id=:id ORDER BY c.revision"""), {"id": item["id"]}).mappings()]
        return item

    def list(self, execution_uuid=None, execution_id=None, query=None, origin=None, classification_state=None, processing_state=None, before=None, limit=50):
        predicates = []
        parameters = {"limit": limit+1}
        for name, expression, value in [
            ("execution_uuid","a.execution_uuid=:execution_uuid",execution_uuid),
            ("execution_id","info.execution_public_id=:execution_id",execution_id),
            ("origin","a.origin=:origin",origin),
            ("processing_state","a.processing_state=:processing_state",processing_state),
            ("before","a.id<:before",before),
        ]:
            if value is not None:
                predicates.append(expression)
                parameters[name] = value
        if query:
            predicates.append("(a.archive_code ILIKE :query OR a.test_name ILIKE :query OR a.actual_object ILIKE :query OR info.execution_code ILIKE :query)")
            parameters["query"] = "%"+query.replace("%", "\\%").replace("_", "\\_")+"%"
        if classification_state:
            predicates.append("a.classification_revision IS "+("NULL" if classification_state=="UNCLASSIFIED" else "NOT NULL"))
        sql = DETAIL+(" WHERE "+" AND ".join(predicates) if predicates else "")+" ORDER BY a.id DESC LIMIT :limit"
        rows = self.session.execute(text(sql),parameters).mappings().all()
        return {"items": [dict(row) for row in rows[:limit]], "next_cursor": rows[limit-1]["cursor"] if len(rows)>limit else None}

    def create(self, payload: ArchiveCreate, key):
        public_id = self.session.execute(text("SELECT integration.archive_create(CAST(:payload AS jsonb),:key)"),
            {"payload": payload.model_dump_json(), "key": key}).scalar_one()
        return self.get(public_id)

    def import_match(self, sha256: str, size_bytes: int):
        # SELECT is subject to existing archive RLS; no hidden-record hint leaks.
        archive_id = self.session.execute(text("""SELECT public_id FROM integration.execution_archive
            WHERE sha256=:sha256 AND size_bytes=:size_bytes ORDER BY id LIMIT 1"""),
            {"sha256": sha256, "size_bytes": size_bytes}).scalar_one_or_none()
        return {"archive": self.get(archive_id) if archive_id else None}

    def import_create(self, payload: ArchiveImportCreate, key: str):
        result = self.session.execute(text("SELECT * FROM integration.archive_import(CAST(:payload AS jsonb),:key)"),
            {"payload": payload.model_dump_json(), "key": key}).mappings().one()
        return {"archive": self.get(result["archive_id"]), "reused": result["reused"]}

    def edit(self, reference, operation, payload: ArchiveVerification | ArchiveClassification, key):
        current = self.get(reference)
        public_id = self.session.execute(text("SELECT integration.archive_edit(:id,:operation,CAST(:payload AS jsonb),:key)"),
            {"id": current["id"], "operation": operation,"payload": payload.model_dump_json(),"key": key}).scalar_one()
        return self.get(public_id,history=True)

    def lock_upload(self, reference):
        current = self.get(reference)
        # Function enforces role/ownership and holds the row lock through commit.
        self.session.execute(text("SELECT integration.archive_lock(:id)"), {"id": current["id"]})
        return self.get(current["id"])

    def update_upload(self, public_id, received, state):
        self.session.execute(text("SELECT integration.archive_upload(:id,:received,:state)"),
            {"id": public_id,"received": received,"state": state})
        return self.get(public_id)

    def submit(self, reference, payload: ArchiveSubmit, key):
        current = self.get(reference)
        # Serialization also prevents verification/classification changing while
        # the existing approval function records its immutable revision snapshot.
        self.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:uuid AS text),38))"), {"uuid": str(current["execution_uuid"])})
        self.session.execute(text("SELECT integration.archive_lock(:id)"), {"id": current["id"]})
        current = self.get(current["id"])
        if current["revision"]!=payload.expected_revision:
            raise ApiError(409,"archive_revision_conflict","核实或归类版本已变化，请刷新。")
        if not current["is_owner"]:
            raise ApiError(403,"archive_owner_required","请由原提交人送审；管理员可以代为补齐归类和核实。")
        missing = [key for key in ("active_seconds", "completed_cycles", "exception_count") if current["facts"].get(key) is None]
        if current["facts"].get("machine_status") == "unknown":
            missing.append("machine_status")
        if current["origin"] == "HISTORY" and missing:
            raise ApiError(422, "history_acquisition_facts_missing",
                "历史 CSV 缺少运行秒数、循环数、异常数或机器终态；可核实归档查看曲线，不能送审。",
                [{"field": key, "code": "unknown_acquisition_fact"} for key in missing])
        canonical = self.session.execute(text("SELECT integration.archive_summary(id) FROM integration.execution_archive WHERE public_id=:id"), {"id": current["id"]}).scalar_one()
        summary = TestSummary.model_validate(canonical)
        SubmissionRepository(self.session).submit(SubmissionCreate(summary=summary,expected_revision=payload.expected_submission_revision,reason=payload.reason),key)
        return self.get(current["id"],history=True)

    def analyses(self, reference):
        archive = self.get(reference)
        rows = self.session.execute(text(ANALYSIS_DETAIL+" WHERE a.public_id=:id ORDER BY av.version"),{"id":archive["id"]}).mappings()
        return {"items":[dict(row) for row in rows],"original_analysis":archive["manifest"].get("analysis")}

    def add_analysis(self, reference, payload: ArchiveAnalysisCreate, key):
        archive = self.get(reference)
        # Owner/admin authorization is independent of approval state: an approved
        # record may gain a reference analysis, but no publication fields change.
        self.session.execute(text("SELECT integration.archive_lock(:id)"),{"id":archive["id"]})
        archive = self.get(archive["id"])
        receipt = self.session.execute(text(ANALYSIS_DETAIL+" WHERE av.actor_id=iam.current_user_id() AND av.idempotency_key=:key"),{"key":key}).mappings().first()
        if receipt is not None:
            receipt_archive = self.session.execute(text("SELECT a.public_id FROM integration.archive_analysis_version av JOIN integration.execution_archive a ON a.id=av.archive_id WHERE av.public_id=:id"),{"id":receipt["id"]}).scalar_one()
            if receipt_archive!=archive["id"] or receipt["source_test_case_version_id"]!=payload.test_case_version_id or receipt["reason"]!=payload.reason:
                raise ApiError(409,"analysis_idempotency_conflict","重新分析幂等键内容不同，请保留原请求或使用新操作。")
            return dict(receipt)
        source = self.session.execute(text("""SELECT v.public_id AS id,v.version,v.decision_rules,v.sampling_requirements,tc.case_code,tc.name
            FROM catalog.test_case_version v JOIN catalog.test_case tc ON tc.id=v.test_case_id
            WHERE v.public_id=:id AND v.status='PUBLISHED' AND tc.enabled FOR SHARE OF v,tc"""),{"id":payload.test_case_version_id}).mappings().first()
        if source is None:
            raise ApiError(422,"published_case_required","请选择真实已发布且启用的用例版本。")
        analyzed = retrospective_analysis(archive,dict(source))
        analysis_id = self.session.execute(text("""SELECT integration.archive_add_analysis(:id,:source,CAST(:acceptance AS jsonb),:status,:notes,CAST(:report AS jsonb),:reason,:key)"""),
            {"id":archive["id"],"source":payload.test_case_version_id,"acceptance":json.dumps(analyzed["acceptance"],ensure_ascii=False,allow_nan=False),
             "status":analyzed["mapping_status"],"notes":analyzed["mapping_notes"],"report":json.dumps(analyzed["report"],ensure_ascii=False,allow_nan=False),"reason":payload.reason,"key":key}).scalar_one()
        row = self.session.execute(text(ANALYSIS_DETAIL+" WHERE av.public_id=:id"),{"id":analysis_id}).mappings().one()
        return dict(row)
