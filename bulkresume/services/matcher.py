import logging

from .deterministic_match import deterministic_match
from .llm_extractor import match_resume_to_jd

logger = logging.getLogger('bulkresume')

# How much each category of requirement counts toward the overall score.
# Must-have skills and meeting the experience bar matter most; good-to-have
# skills, qualifications, and location matter but shouldn't dominate.
_CATEGORY_WEIGHT = {
    "must_have": 3,
    "experience": 2,
    "good_to_have": 1,
    "qualification": 1,
    "location": 1,
    "other": 1,
}


def _classify_field(entry: dict, jd: dict) -> str:
    """Best-effort classification of one field_breakdown row against which
    JD list/category it came from, so reconcile_match_result() can weight
    must-have skills more heavily than a nice-to-have, a qualification, or
    location."""
    field = (entry.get("field") or "").strip().lower()
    jd_requirement = (entry.get("jd_requirement") or "").strip().lower()

    if "experience" in field:
        return "experience"
    if "location" in field:
        return "location"
    if "qualification" in field:
        return "qualification"

    for skill in jd.get("must_have_skills", []):
        if skill.strip().lower() == jd_requirement:
            return "must_have"
    for skill in jd.get("good_to_have_skills", []):
        if skill.strip().lower() == jd_requirement:
            return "good_to_have"
    for qual in jd.get("qualifications", []):
        if qual.strip().lower() == jd_requirement:
            return "qualification"
    return "other"


def reconcile_match_result(jd: dict, result: dict) -> dict:
    """
    The LLM's field_breakdown (one row per JD requirement, each independently
    scored matched=true/false + its own 0-100 match_percent) is far more
    reliable than the LLM's own top-level overall_match_percent /
    matched_skills / missing_skills / recommendation fields -- those get
    silently zeroed out whenever the model's response omits them, because
    schema strict-mode is intentionally off (see llm_extractor.py -- turning
    it on causes Groq to reject the ENTIRE response if even one field/array
    item is missing, which was worse). An omitted key just falls back to
    Pydantic's empty default ("", 0.0, []) instead of failing loudly, so a
    response can show field_breakdown entries with matched=true and still
    report overall_match_percent=0.0 / matched_skills=[] / recommendation=""
    -- exactly the bug this fixes.

    This recomputes those fields deterministically FROM field_breakdown
    instead of trusting the LLM's separate aggregate numbers, so "some
    requirements matched" always shows up in the overall score.
    """
    field_breakdown = result.get("field_breakdown") or []
    if not field_breakdown:
        # Nothing to reconcile against -- this is the deterministic-fallback
        # path (services/deterministic_match.py), which doesn't produce a
        # field_breakdown at all. Leave it as-is.
        return result

    weighted_sum = 0.0
    weight_total = 0.0
    matched_skills, missing_skills = [], []
    matched_quals, missing_quals = [], []

    for entry in field_breakdown:
        category = _classify_field(entry, jd)
        weight = _CATEGORY_WEIGHT.get(category, 1)

        pct = entry.get("match_percent")
        pct = float(pct) if isinstance(pct, (int, float)) else (100.0 if entry.get("matched") else 0.0)

        weighted_sum += weight * pct
        weight_total += weight

        label = entry.get("jd_requirement") or entry.get("field") or ""
        if category in ("must_have", "good_to_have"):
            (matched_skills if entry.get("matched") else missing_skills).append(label)
        elif category == "qualification":
            (matched_quals if entry.get("matched") else missing_quals).append(label)

    overall = round(weighted_sum / weight_total, 1) if weight_total else 0.0

    result["overall_match_percent"] = overall
    result["matched_skills"] = matched_skills
    result["missing_skills"] = missing_skills
    result["matched_qualifications"] = matched_quals
    result["missing_qualifications"] = missing_quals
    result["recommendation"] = (
        "Strong Match" if overall >= 75 else "Partial Match" if overall >= 40 else "Weak Match"
    )
    # strengths_summary/gaps_summary are prose, not scalars the model tends
    # to skip -- but if the model DID leave one blank, synthesize a plain
    # one from the (now-reliable) matched/missing lists rather than showing
    # an empty string.
    if not result.get("strengths_summary"):
        combined = matched_skills + matched_quals
        result["strengths_summary"] = f"Matches: {', '.join(combined)}." if combined else "No requirements matched."
    if not result.get("gaps_summary"):
        combined = missing_skills + missing_quals
        result["gaps_summary"] = f"Missing: {', '.join(combined)}." if combined else "No gaps identified."

    return result


def match_candidate_against_jd(jd_data: dict, candidate_data: dict, log_prefix: str = "") -> dict:
    """
    Tries the semantic (LLM) matcher first — it understands synonyms,
    seniority, and context ("Node.js" satisfying a JD asking for "backend
    JavaScript experience", "4 years" satisfying "3+ years required") in a
    way a pure keyword match cannot. Falls back to the deterministic
    skill-overlap matcher (services/deterministic_match.py) if the LLM call
    fails, so a Groq outage degrades match quality instead of breaking the
    endpoint outright.

    Either way, the result is passed through reconcile_match_result() so
    the overall score/matched-skills/recommendation are always derived
    consistently from the underlying per-requirement data rather than a
    separate (and sometimes-omitted) top-level LLM field.

    Returns a plain dict shaped like ResumeJDMatchResult.model_dump() plus a
    "match_method" key ("llm" or "deterministic_fallback") so callers/API
    consumers can see which path produced a given result.
    """
    result, needs_review = match_resume_to_jd(jd_data, candidate_data)

    if needs_review:
        logger.warning(f"{log_prefix}LLM match failed, using deterministic fallback")
        fallback = deterministic_match(jd_data, candidate_data)
        fallback["match_method"] = "deterministic_fallback"
        return fallback

    data = result.model_dump()
    data["match_method"] = "llm"
    data = reconcile_match_result(jd_data, data)
    return data