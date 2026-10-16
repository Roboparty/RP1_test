import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.schemas.submissions import SubmissionCreate, SubmissionReview


class SubmissionRepository:
    def __init__(self, session: Session):
        self.session = session

    def list(self, status: str | None, before: int | None, limit: int):
        rows = self.session.execute(text("""
            SELECT s.public_id AS id,s.id AS cursor,s.revision,s.status,s.created_at,s.updated_at,
                   u.username AS submitter,u.display_name AS submitter_name,c.campaign_code,
                   iam.can_write_business() AS can_review
            FROM integration.data_submission s
            JOIN LATERAL integration.submission_person(s.submitter_id) u ON true JOIN test.test_campaign c ON c.id=s.campaign_id
            WHERE (CAST(:status AS text) IS NULL OR s.status=CAST(:status AS text))
              AND (CAST(:before AS bigint) IS NULL OR s.id<CAST(:before AS bigint))
            ORDER BY s.id DESC LIMIT :limit
        """), {"status": status, "before": before, "limit": limit + 1}).mappings().all()
        return {"items": [dict(r) for r in rows[:limit]], "next_cursor": rows[limit - 1]["cursor"] if len(rows) > limit else None}

    def get(self, submission_id: UUID):
        row = self.session.execute(text("""
            SELECT s.public_id AS id,s.execution_uuid,s.revision,s.status,s.created_at,s.updated_at,
                   u.username AS submitter,u.display_name AS submitter_name,c.campaign_code,
                   iam.can_write_business() AS can_review,
                   s.submitter_id=iam.current_user_id() AS is_owner,
                   e.public_id AS execution_id,e.execution_code,
                   ar.public_id AS archive_id,ar.archive_code,
                   a.asset_code,a.display_name AS asset_name,tc.case_code,tc.name AS case_name,
                   version.version AS test_case_version,station.station_code,cfg.fingerprint AS configuration_fingerprint
            FROM integration.data_submission s JOIN LATERAL integration.submission_person(s.submitter_id) u ON true
            JOIN test.test_campaign c ON c.id=s.campaign_id
            JOIN integration.data_submission_revision current_revision ON current_revision.submission_id=s.id AND current_revision.revision=s.revision
            JOIN test.asset a ON a.public_id=(current_revision.payload->>'asset_id')::uuid
            JOIN catalog.test_case_version version ON version.public_id=(current_revision.payload->>'test_case_version_id')::uuid
            JOIN catalog.test_case tc ON tc.id=version.test_case_id
            JOIN test.station station ON station.public_id=(current_revision.payload->>'station_id')::uuid
            JOIN test.configuration_snapshot cfg ON cfg.public_id=(current_revision.payload->>'configuration_id')::uuid
            LEFT JOIN test.test_execution e ON e.approved_submission_id=s.id
            LEFT JOIN integration.execution_archive ar ON ar.execution_uuid=s.execution_uuid
            WHERE s.public_id=:id
        """), {"id": submission_id}).mappings().first()
        if row is None:
            raise ApiError(404, "not_found", "提交不存在或无权查看。")
        revisions = self.session.execute(text("""
            SELECT v.revision,v.payload AS summary,v.reason AS submission_reason,v.submitted_at,
                   r.decision,r.reason AS review_reason,r.reviewed_at,u.username AS reviewer,
                   av.payload AS verification,ar.manifest AS analysis
            FROM integration.data_submission_revision v
            JOIN integration.data_submission s ON s.id=v.submission_id
            LEFT JOIN integration.data_submission_review r ON r.submission_id=v.submission_id AND r.revision=v.revision
            LEFT JOIN LATERAL integration.submission_person(r.reviewer_id) u ON true
            LEFT JOIN integration.archive_submission snap ON snap.submission_id=v.submission_id AND snap.submission_revision=v.revision
            LEFT JOIN integration.archive_verification av ON av.archive_id=snap.archive_id AND av.revision=snap.verification_revision
            LEFT JOIN integration.execution_archive ar ON ar.id=snap.archive_id
            WHERE s.public_id=:id ORDER BY v.revision
        """), {"id": submission_id}).mappings()
        return {**dict(row), "revisions": [dict(r) for r in revisions]}

    def submit(self, payload: SubmissionCreate, key: str):
        public_id = self.session.execute(text("SELECT integration.submit_test_data(CAST(:payload AS jsonb),:expected,:key,:reason)"), {
            "payload": json.dumps(payload.summary.model_dump(mode="json"), ensure_ascii=False),
            "expected": payload.expected_revision, "key": key, "reason": payload.reason,
        }).scalar_one()
        return self.get(public_id)

    def review(self, submission_id: UUID, payload: SubmissionReview, key: str):
        public_id = self.session.execute(text("SELECT integration.review_test_data(:id,:revision,:decision,:reason,:key)"), {
            "id": submission_id, **payload.model_dump(), "key": key,
        }).scalar_one()
        return self.get(public_id)
