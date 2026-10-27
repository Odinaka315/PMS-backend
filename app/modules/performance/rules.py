"""Appraisal arithmetic and the placeholder defaults. Kept free of the database so it can be tested on its own."""
from fastapi import HTTPException

# PLACEHOLDERS: the hospital has not confirmed its appraisal form or rating bands. HR can override both per cycle.
DEFAULT_CRITERIA = [
    {"key": "job_quality", "name": "Quality of clinical / professional work", "weight": 30},
    {"key": "client_feedback", "name": "Patient and client feedback", "weight": 20},
    {"key": "attendance", "name": "Punctuality and attendance", "weight": 15},
    {"key": "teamwork", "name": "Teamwork and communication", "weight": 15},
    {"key": "training", "name": "Training and development", "weight": 10},
    {"key": "initiative", "name": "Initiative and leadership", "weight": 10},
]
DEFAULT_BANDS = [
    {"min": 90, "label": "Excellent"}, {"min": 75, "label": "Very Good"}, {"min": 60, "label": "Good"},
    {"min": 50, "label": "Fair"}, {"min": 0, "label": "Poor"},
]

APPRAISER_ROLES = ("hod", "hr", "director", "superadmin")


def validate_scores(scores: dict[str, int], criteria: list[dict]) -> dict[str, int]:
    keys = {c["key"] for c in criteria}
    if set(scores) != keys:
        missing, extra = sorted(keys - set(scores)), sorted(set(scores) - keys)
        raise HTTPException(422, "Scores must cover every criterion exactly once"
                                 + (f"; missing: {', '.join(missing)}" if missing else "")
                                 + (f"; unknown: {', '.join(extra)}" if extra else ""))
    for k, v in scores.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
            raise HTTPException(422, f"Score for '{k}' must be a whole number from 0 to 100")
    return scores


def weighted_x100(scores: dict[str, int], criteria: list[dict]) -> int:
    """Weights add up to 100, so sum(score * weight) is the weighted average in hundredths (9150 = 91.50)."""
    return sum(scores[c["key"]] * c["weight"] for c in criteria)


def rating_for(score_x100: int, bands: list[dict]) -> str:
    for b in sorted(bands, key=lambda b: -b["min"]):
        if score_x100 >= b["min"] * 100:
            return b["label"]
    return bands[-1]["label"]
