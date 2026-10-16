ALTER TABLE integration.data_submission_revision
    DROP CONSTRAINT data_submission_revision_reason_check;
ALTER TABLE integration.data_submission_revision
    ADD CONSTRAINT data_submission_revision_reason_check
    CHECK (length(btrim(reason)) BETWEEN 1 AND 300);

ALTER TABLE integration.data_submission_review
    DROP CONSTRAINT data_submission_review_reason_check;
ALTER TABLE integration.data_submission_review
    ADD CONSTRAINT data_submission_review_reason_check
    CHECK (length(btrim(reason)) BETWEEN 1 AND 300);
