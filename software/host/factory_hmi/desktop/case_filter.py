"""Keep the platform case list aligned with the selected limb."""
from __future__ import annotations

from typing import Mapping

# Case codes use the full-degree part (ARM, LEG). Sample codes keep S for the
# reduced-degree hardware. A limb may use the full-degree cases with only its
# own sample segment.
LIMB_PARTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "left_arm": (frozenset({"ARM"}), frozenset({"ARM"})),
    "right_arm": (frozenset({"ARM"}), frozenset({"ARM"})),
    "left_short_arm": (frozenset({"ARM", "SARM"}), frozenset({"SARM"})),
    "right_short_arm": (frozenset({"ARM", "SARM"}), frozenset({"SARM"})),
    "left_leg": (frozenset({"LEG"}), frozenset({"LEG"})),
    "right_leg": (frozenset({"LEG"}), frozenset({"LEG"})),
    "left_short_leg": (frozenset({"LEG", "SLEG"}), frozenset({"SLEG"})),
    "right_short_leg": (frozenset({"LEG", "SLEG"}), frozenset({"SLEG"})),
    "upper_body": (frozenset({"UPPER"}), frozenset({"UPPER"})),
    "biped_waist": (frozenset({"LOWER"}), frozenset({"LOWER"})),
    "waist_hip": (frozenset({"LOWER"}), frozenset({"LOWER"})),
    "lower_body": (frozenset({"LOWER"}), frozenset({"LOWER"})),
}


def case_part(code: str) -> str:
    text = str(code or "").strip().upper()
    if text.startswith("SYS-"):
        return "SYS"
    pieces = text.split("-")
    return pieces[1] if len(pieces) >= 2 else ""


def sample_part(code: str) -> str:
    pieces = str(code or "").strip().upper().split("-")
    return pieces[1] if len(pieces) >= 3 and pieces[0].startswith("RP") else ""


LIMB_LABELS: tuple[tuple[str, str], ...] = (
    ("left_arm", "左臂（7自由度）"),
    ("right_arm", "右臂（7自由度）"),
    ("left_short_arm", "左臂（5自由度）"),
    ("right_short_arm", "右臂（5自由度）"),
    ("left_leg", "左腿（6自由度）"),
    ("right_leg", "右腿（6自由度）"),
    ("left_short_leg", "左腿（5自由度）"),
    ("right_short_leg", "右腿（5自由度）"),
    ("waist_hip", "腰髋"),
    ("biped_waist", "双足腰"),
    ("upper_body", "上肢"),
    ("lower_body", "下肢"),
)


def limb_label(key: str) -> str:
    for code, label in LIMB_LABELS:
        if code == key:
            return label
    return key or "未配置"


OTHER_CASE = "OTHER"


def start_decision(
    *,
    saved_bench: str,
    context_bench: str,
    case_code: str | None,
    has_context: bool,
) -> str:
    """Decide whether a start has a case and a matching bench."""
    code = str(case_code or "").strip()
    if not code:
        return "need_case"
    if code == OTHER_CASE:
        if not saved_bench.strip():
            return "need_bench"
        return "local_other"
    if not has_context:
        return "need_sample"
    bench = saved_bench.strip().upper()
    expected = context_bench.strip().upper()
    if not bench:
        return "need_bench"
    if not expected or bench != expected:
        return "bench_mismatch"
    return "ready"


def profile_matches_limb(profile: Mapping[str, object], limb: str) -> bool:
    spec = LIMB_PARTS.get(str(limb or "").strip())
    if spec is None:
        return True
    case_parts, sample_parts = spec
    case = case_part(str(profile.get("case_code") or profile.get("test_case_id") or ""))
    if case not in case_parts:
        return False
    sample = sample_part(str(profile.get("sample_id") or profile.get("asset_code") or ""))
    return not sample or sample in sample_parts
