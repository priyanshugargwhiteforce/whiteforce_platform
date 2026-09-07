"""
PARSE SCORE
------------
Scores how good a completed extraction is, on a 0-100 scale, so
pipeline.py can decide whether the result is trustworthy or needs the
OCR deep-dive retry.

Two signals, combined:

  A) FIELD COVERAGE (65%) — did extract_structured_data() actually
     populate the fields that matter? A resume with empty skills AND
     empty education AND empty experience/internships is almost always
     a parsing failure (bad OCR/garbled text/corrupted layer), not a
     genuinely blank resume — nobody submits a resume with none of those.

  B) TEXT QUALITY (35%) — even when the LLM manages to fill some fields,
     the underlying raw_text might be garbled (broken font encoding in
     the PDF, a mostly-image PDF classified as pdf_native because it
     technically had >30 chars/page from a logo or header, etc). A low
     text-quality score here means the *source text itself* was bad,
     independent of what the LLM managed to salvage from it — this is
     exactly the case OCR deep-dive is meant to catch.

This module has NO Django/Groq/OCR dependencies — pure functions over
plain data — so it's easy to unit test and safe to import from anywhere
in the pipeline without dependency risk.
"""

import re
from dataclasses import dataclass, field


@dataclass
class ScoreBreakdown:
    total_score: float
    field_coverage_score: float
    text_quality_score: float
    details: dict = field(default_factory=dict)

    def needs_ocr_deep_dive(self, threshold: float = 55.0) -> bool:
        return self.total_score <= threshold


# Weight of each field in the coverage score. Sums to 1.0.
# Contact info (email/phone) and skills/education are the highest-signal
# fields — almost every real resume has these; missing them together is
# the strongest indicator of a bad source-text extraction.
FIELD_WEIGHTS = {
    "contact": 0.15,       # email OR phone present
    "name": 0.10,
    "skills": 0.20,
    "education": 0.20,
    "experience_or_internship": 0.15,   # either counts — freshers may have only internships
    "profile_summary": 0.05,
    "candidate_address": 0.05,
    "known_languages": 0.03,
    "linkedin_or_urls": 0.03,
    "certifications": 0.04,
}


def _field_coverage_score(extracted) -> tuple[float, dict]:
    """
    `extracted` is a bulkresume.services.schemas.ResumeExtraction instance
    (or anything with the same attribute names — regex_fallback's dict-based
    output, once passed through ResumeExtraction(**data), also satisfies this).
    """
    has = {
        "contact": bool(extracted.email or extracted.phone),
        "name": bool(extracted.name),
        "skills": bool(extracted.skills),
        "education": bool(extracted.education),
        "experience_or_internship": bool(extracted.experience or extracted.internships),
        "profile_summary": bool(extracted.profile_summary),
        "candidate_address": bool(extracted.candidate_address),
        "known_languages": bool(extracted.known_languages),
        "linkedin_or_urls": bool(extracted.linkedin_url or extracted.other_urls),
        "certifications": bool(extracted.certifications),
    }
    score = sum(FIELD_WEIGHTS[k] * 100 for k, present in has.items() if present)
    return score, has


# ---------------- TEXT QUALITY (same heuristics as before, reused) ----------------
PRINTABLE_RE = re.compile(r"[\x20-\x7E\n\t]")
ALPHA_RE = re.compile(r"[A-Za-z]")
GARBAGE_RE = re.compile(r"[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]")


def _text_quality_score(raw_text: str) -> tuple[float, dict]:
    details = {}
    if not raw_text or len(raw_text.strip()) < 20:
        details["reason"] = "text_too_short_or_empty"
        return 0.0, details

    n = len(raw_text)
    printable_ratio = len(PRINTABLE_RE.findall(raw_text)) / n
    alpha_ratio = len(ALPHA_RE.findall(raw_text)) / n
    garbage_ratio = len(GARBAGE_RE.findall(raw_text)) / n
    whitespace_ratio = raw_text.count(" ") / n

    words = raw_text.split()
    avg_word_len = sum(len(w) for w in words) / len(words) if words else 0

    details.update({
        "printable_ratio": round(printable_ratio, 3),
        "alpha_ratio": round(alpha_ratio, 3),
        "garbage_ratio": round(garbage_ratio, 3),
        "whitespace_ratio": round(whitespace_ratio, 3),
        "avg_word_len": round(avg_word_len, 2),
        "word_count": len(words),
    })

    checks = [
        min(printable_ratio / 0.95, 1.0) * 100,
        min(alpha_ratio / 0.55, 1.0) * 100,
        max(0.0, 1.0 - garbage_ratio / 0.02) * 100,
        100 if 0.10 <= whitespace_ratio <= 0.25 else 50,
        100 if 2.5 <= avg_word_len <= 9 else 40,
        100 if len(words) >= 80 else (len(words) / 80) * 100,
    ]
    return sum(checks) / len(checks), details


def compute_parse_score(extracted, raw_text: str) -> ScoreBreakdown:
    field_score, field_details = _field_coverage_score(extracted)
    quality_score, quality_details = _text_quality_score(raw_text)
    total = (field_score * 0.65) + (quality_score * 0.35)

    return ScoreBreakdown(
        total_score=round(total, 2),
        field_coverage_score=round(field_score, 2),
        text_quality_score=round(quality_score, 2),
        details={"fields": field_details, "text_quality": quality_details},
    )


if __name__ == "__main__":
    # Standalone smoke test — schemas.py has no Django/Groq dependency,
    # so this runs without any settings/DB/env configured.
    from schemas import ResumeExtraction, Education, Experience

    good = ResumeExtraction(
        name="Priyanshu Sharma", email="p@example.com", phone="+919999999999",
        skills=["Python", "Django"],
        education=[Education(degree="B.Tech", institution="XYZ University", year="2022")],
        experience=[Experience(title="Backend Developer", company="Acme", duration="2023-Present")],
        profile_summary="Backend developer with Django experience.",
    )
    good_text = "Priyanshu Sharma Backend Developer with experience in Python and Django. " * 15

    bad = ResumeExtraction()  # everything empty -- simulates a failed extraction
    bad_text = "J\ufffdhn D\x00e S\ufffdftw\x0bre \ufffd\ufffd\ufffd###@@@%%%"

    for label, extracted, text in [("GOOD", good, good_text), ("BAD", bad, bad_text)]:
        result = compute_parse_score(extracted, text)
        print(f"--- {label} --- score={result.total_score} needs_ocr={result.needs_ocr_deep_dive()}")
        print(result.details)