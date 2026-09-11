"""
Deterministic, LLM-free fallback matcher. Used when match_resume_to_jd()
(services/llm_extractor.py) fails, so a Groq outage never turns a
resume<->JD match into a total endpoint failure — it just means a cruder
(but still real) match percentage instead of a semantic one.

Purely set-overlap based: case-insensitive, substring-tolerant comparison
of candidate skills against the JD's must-have / good-to-have skill lists.
No extra dependencies (no fuzzy-matching libraries) beyond stdlib.
"""
from typing import List


def _normalize(items: List[str]) -> set:
    return {i.strip().lower() for i in items if i and i.strip()}


def _fuzzy_contains(needle: str, haystack: set) -> bool:
    """True if `needle` matches something in `haystack` exactly, or as a
    substring in either direction (e.g. 'react' matches 'react.js')."""
    if needle in haystack:
        return True
    return any(needle in h or h in needle for h in haystack)


def deterministic_match(jd: dict, candidate: dict) -> dict:
    """Returns a dict shaped like ResumeJDMatchResult.model_dump() (see
    services/schemas.py), minus the fields that genuinely need semantic
    judgment (field_breakdown, qualification/experience comparison) —
    those are left empty/neutral rather than guessed at."""
    must_have = _normalize(jd.get("must_have_skills", []))
    good_to_have = _normalize(jd.get("good_to_have_skills", []))
    candidate_skills = _normalize(candidate.get("skills", []))

    matched_must = {s for s in must_have if _fuzzy_contains(s, candidate_skills)}
    missing_must = must_have - matched_must
    matched_good = {s for s in good_to_have if _fuzzy_contains(s, candidate_skills)}
    missing_good = good_to_have - matched_good

    # Weighting: must-have skills count for 80% of the score, good-to-have
    # for the remaining 20% — mirrors how a recruiter would weigh a
    # "required" vs "nice to have" line on a JD. If the JD has neither list
    # populated, don't zero the whole score out just because of an empty JD.
    must_score = (len(matched_must) / len(must_have) * 80) if must_have else 80
    good_score = (len(matched_good) / len(good_to_have) * 20) if good_to_have else 20
    overall = round(must_score + good_score, 2)

    return {
        "overall_match_percent": overall,
        "matched_skills": sorted(matched_must | matched_good),
        "missing_skills": sorted(missing_must | missing_good),
        "matched_qualifications": [],
        "missing_qualifications": [],
        "experience_match": "Not evaluated (deterministic fallback compares skills only)",
        "field_breakdown": [],
        "strengths_summary": (
            f"Matches {len(matched_must)}/{len(must_have)} must-have and "
            f"{len(matched_good)}/{len(good_to_have)} good-to-have skills."
            if (must_have or good_to_have)
            else "No skill requirements found on the JD to compare against."
        ),
        "gaps_summary": f"Missing skills: {', '.join(sorted(missing_must | missing_good)) or 'none'}.",
        "recommendation": (
            "Strong Match" if overall >= 75 else "Partial Match" if overall >= 40 else "Weak Match"
        ),
    }
