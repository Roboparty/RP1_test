from __future__ import annotations

import re
from datetime import datetime
import uuid
from zoneinfo import ZoneInfo


_CASE_ID = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_SAMPLE_ID = re.compile(r"^RP[0-9]+\.[0-9]+-(?:ARM|SARM|LEG|SLEG|SYS|UPPER|LOWER|CHEST|HEAD|BAT)-[0-9]{3}$")
_BENCH_ID = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")


def build_execution_code(
    test_case_id: str,
    started_at: datetime,
    sample_id: str,
    bench_id: str,
) -> str:
    """Check the platform code shape before a run starts.

    The returned prefix is not an execution number. PostgreSQL assigns the
    official value, including the ``_E<id>`` suffix, and the workstation
    displays that returned value.
    """
    case = test_case_id.strip().upper()
    sample = sample_id.strip().upper()
    bench = bench_id.strip().upper()
    if not _CASE_ID.fullmatch(case):
        raise ValueError("test_case_id must be a published case code, for example REL-SARM-001；不能使用 left_arm 等部位名")
    if not _SAMPLE_ID.fullmatch(sample):
        raise ValueError(
            "sample_id must match RP<产品>.<版本>-<部位>-<三位序号>, "
            "for example RP1.3-SARM-001"
        )
    if not _BENCH_ID.fullmatch(bench):
        raise ValueError("bench_id must match the platform station code, for example MP-01")
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("started_at must include a timezone")
    stamp = started_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%y%m%d%H%M")
    code = "_".join((case, stamp, sample, bench))
    if len(code) > 209:
        raise ValueError("execution code is too long for a unique local record identifier")
    return code


def build_local_execution_code(
    test_case_id: str, started_at: datetime, execution_uuid: str
) -> str:
    """Name a local record without inventing a platform sample or bench ID."""
    case = re.sub(r"[^A-Za-z0-9_.-]+", "_", test_case_id).strip("._")[:64] or "aging"
    execution = uuid.UUID(execution_uuid)
    return f"LOCAL_{case}_{started_at.strftime('%y%m%d%H%M%S%f')}_{execution.hex}"
