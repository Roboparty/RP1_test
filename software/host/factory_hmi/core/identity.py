from __future__ import annotations

import re
from datetime import datetime


_SAMPLE_ID = re.compile(
    r"^[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)*-[A-Z][A-Z0-9]*-[A-Z0-9]{2,16}$"
)
_BENCH_ID = re.compile(r"^(?:RD|MP|OUT)-\d{2,}$")


def build_execution_code(
    test_case_id: str,
    started_at: datetime,
    sample_id: str,
    bench_id: str,
) -> str:
    """Build the platform's canonical public execution identifier."""
    case = test_case_id.strip()
    sample = sample_id.strip().upper()
    bench = bench_id.strip().upper()
    if not case or any(character.isspace() for character in case):
        raise ValueError("test_case_id must be non-empty and cannot contain whitespace")
    if not _SAMPLE_ID.fullmatch(sample):
        raise ValueError(
            "sample_id must match PRODUCT.VERSION-PART-SERIAL, "
            "for example RP1.3-LOWER-01"
        )
    if not _BENCH_ID.fullmatch(bench):
        raise ValueError("bench_id must match RD|MP|OUT-NN, for example MP-01")
    return "_".join((case, started_at.strftime("%y%m%d%H%M"), sample, bench))
