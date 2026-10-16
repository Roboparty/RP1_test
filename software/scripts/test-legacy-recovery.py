#!/usr/bin/env python3
"""Exercise recovery against a disposable restored PostgreSQL container only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if not args.container.startswith("rp1-legacy-recovery-check-"):
        parser.error("only disposable rp1-legacy-recovery-check-* containers are accepted")
    spec = importlib.util.spec_from_file_location("recovery", Path(__file__).with_name("recover-legacy-summaries.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = json.loads((args.directory / "prepared.json").read_text())
    sql = lambda statement: module.psql(args.container, statement)
    count_sql = """SELECT json_build_object(
      'batches',(SELECT count(*) FROM integration.legacy_recovery_batch),
      'executions',(SELECT count(*) FROM integration.legacy_recovered_execution),
      'summaries',(SELECT count(*) FROM integration.legacy_joint_summary),
      'events',(SELECT count(*) FROM integration.legacy_recovered_event),
      'audit',(SELECT count(*) FROM audit.change_log))"""
    checks = []
    first = module.apply(args.directory, args.container, data["dataset_sha256"], "admin")
    before = sql(count_sql)
    second = module.apply(args.directory, args.container, data["dataset_sha256"], "admin")
    assert first == second and before == sql(count_sql), "retry changed records/audit"
    checks += ["full_import_and_formal_table_fingerprints", "exact_retry_no_records_or_audit_added"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(module.apply, args.directory, args.container,
                               data["dataset_sha256"], "admin") for _ in range(2)]
        assert all(f.result() == first for f in futures)
    assert before == sql(count_sql), "concurrent retries changed records/audit"
    checks += ["concurrent_retries_serialized_without_duplicates"]

    def rejected(statement):
        try:
            sql(statement)
        except RuntimeError:
            return
        raise AssertionError("expected database rejection")

    for state in ("APPROVED", "REJECTED"):
        rejected(f"UPDATE integration.legacy_recovery_batch SET review_state='{state}'")
    rejected("DELETE FROM integration.legacy_joint_summary")
    checks += ["source_evidence_immutable_no_fake_approval"]

    admin_id = sql("SELECT public_id FROM iam.app_user WHERE username='admin'")
    service_id = sql("SELECT public_id FROM iam.app_user WHERE username='rp1-recompute-worker'")
    rls_prefix = "BEGIN; SET LOCAL ROLE rp1_app; "
    assert sql(rls_prefix + "SELECT count(*) FROM integration.legacy_joint_summary; ROLLBACK;") == "0"
    assert sql(rls_prefix + f"SELECT iam.set_request_context('{service_id}','recovery-test','rls test'); SELECT count(*) FROM integration.legacy_joint_summary; ROLLBACK;").splitlines()[-1] == "0"
    assert sql(rls_prefix + f"SELECT iam.set_request_context('{admin_id}','recovery-test','rls test'); SELECT count(*) FROM integration.legacy_joint_summary; ROLLBACK;").splitlines()[-1] == str(len(data["summaries"]))
    rejected(rls_prefix + "UPDATE integration.legacy_joint_summary SET temp_rise_c=0;")
    checks += ["rls_no_context_and_worker_hidden_admin_visible", "application_role_write_denied"]

    for bad_kind in ("invalid_event", "raw_array", "batch_conflict"):
        with tempfile.TemporaryDirectory(prefix="rp1-recovery-negative-") as tmp:
            altered = json.loads(json.dumps(data))
            if bad_kind == "invalid_event":
                altered["events"][0]["event_type"] = "not-a-valid-event"
            elif bad_kind == "raw_array":
                altered["executions"][0]["source_snapshot"]["points"] = [1, 2, 3]
            else:
                altered["events"][0]["duration_s"] = 123.456
            evidence = dict(altered)
            evidence.pop("batch_key")
            evidence.pop("dataset_sha256")
            altered["dataset_sha256"] = module.digest(evidence)
            altered["batch_key"] = data["batch_key"] if bad_kind == "batch_conflict" else "NEGATIVE-" + bad_kind
            module.private_json(Path(tmp) / "prepared.json", altered)
            try:
                module.apply(Path(tmp), args.container, altered["dataset_sha256"], "admin")
            except RuntimeError:
                pass
            else:
                raise AssertionError("invalid source evidence accepted")
            assert before == sql(count_sql), "failed import leaked rows or audit records"
    checks += ["invalid_event_rolls_back_entire_batch_and_audit", "raw_array_rejected_and_transaction_rolled_back",
               "same_batch_different_content_rejected_without_overwrite"]
    try:
        module.apply(args.directory, args.container, "0" * 64, "admin")
    except ValueError:
        pass
    else:
        raise AssertionError("bad checksum accepted")
    checks += ["checksum_mismatch_rejected_before_write"]
    result = {"passed": True, "checks": checks, "result": first}
    module.private_json(args.directory / "isolated-validation.json", result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
