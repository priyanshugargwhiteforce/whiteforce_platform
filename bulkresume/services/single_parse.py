"""
SYNCHRONOUS SINGLE-RESUME PARSE HELPER
------------------------------------------
Used ONLY by SingleResumeParseView (views.py) — the new "parse this one
file right now and give me the data back in this response" endpoint.

Deliberately does NOT modify services/pipeline.py at all. It imports two
small pieces FROM pipeline.py (clean_resume_text, OCR_DEEP_DIVE_SCORE_THRESHOLD)
instead of redefining them, so the two code paths can never disagree on
what "clean text" or "the deep-dive threshold" means — but pipeline.py's
own process_resume() function, its dedup-check flow, and its Celery task
wiring are completely untouched by this file's existence.

Differences from the async bulk pipeline (services/pipeline.py -> process_resume):
  - NO duplicate-candidate check (check_duplicate_in_db / DUPLICATE_CHECK_API_URL)
    — every file sent to this endpoint gets parsed, always, per requirement.
  - Runs inline in the request/response cycle (no Celery `.delay()`), so the
    caller gets the fully extracted profile back in the SAME HTTP response
    instead of polling a batch-status endpoint.
  - Everything else (LLM extraction, scoring, OCR deep-dive-and-reparse
    retry logic) is identical to the bulk pipeline, so results are
    consistent regardless of which endpoint a resume goes through.
"""

import logging

from .deep_dive_ocr import deep_dive_ocr_extract
from .extractors import extract_text
from .llm_extractor import extract_structured_data
from .parse_score import compute_parse_score
from .pipeline import OCR_DEEP_DIVE_SCORE_THRESHOLD, clean_resume_text

logger = logging.getLogger('bulkresume')


def parse_single_resume(file_path: str, file_type: str, log_prefix: str = "") -> dict:
    """
    Mirrors process_resume()'s LLM-extract -> score -> (maybe) OCR deep-dive
    -> reparse block exactly, minus the dedup check. Returns a plain dict
    (not a dataclass) so the caller in views.py can pass it straight into
    build_profile_defaults() below without an extra import.
    """
    raw_text = extract_text(file_path, file_type)
    raw_text = clean_resume_text(raw_text)

    extracted, needs_review = extract_structured_data(raw_text)
    extraction_method = "regex_fallback" if needs_review else "llm"

    score = compute_parse_score(extracted, raw_text)
    ocr_deep_dive_used = False

    if score.needs_ocr_deep_dive(threshold=OCR_DEEP_DIVE_SCORE_THRESHOLD):
        logger.info(
            f"{log_prefix}parse score {score.total_score} <= "
            f"{OCR_DEEP_DIVE_SCORE_THRESHOLD}, attempting OCR deep-dive"
        )
        try:
            ocr_text = deep_dive_ocr_extract(file_path, file_type)
            if ocr_text:
                ocr_text = clean_resume_text(ocr_text)
                ocr_extracted, ocr_needs_review = extract_structured_data(ocr_text)
                ocr_score = compute_parse_score(ocr_extracted, ocr_text)

                logger.info(
                    f"{log_prefix}deep-dive score {ocr_score.total_score} "
                    f"vs original {score.total_score}"
                )

                if ocr_score.total_score > score.total_score:
                    extracted = ocr_extracted
                    needs_review = ocr_needs_review
                    extraction_method = "regex_fallback" if needs_review else "llm_ocr_deep_dive"
                    raw_text = ocr_text
                    score = ocr_score
                    ocr_deep_dive_used = True
            else:
                logger.info(f"{log_prefix}deep-dive OCR produced no usable text, keeping original")
        except Exception as exc:
            logger.warning(f"{log_prefix}OCR deep-dive raised {exc!r}, keeping original result")

    return {
        "extracted": extracted,
        "raw_text": raw_text,
        "needs_review": needs_review,
        "extraction_method": extraction_method,
        "score": score,
        "ocr_deep_dive_used": ocr_deep_dive_used,
    }


def build_profile_defaults(extracted, needs_review: bool, extraction_method: str,
                            score, ocr_deep_dive_used: bool) -> dict:
    """
    Same field mapping pipeline.py uses in its ParsedProfile.objects.update_or_create
    call. Duplicated here (intentionally, as a plain data-mapping function with
    no side effects) rather than importing it from pipeline.py, since pipeline.py
    doesn't currently expose it as a standalone function — this keeps that file's
    working, already-tested update_or_create call completely untouched.
    """
    return {
        "name": extracted.name,
        "email": extracted.email,
        "phone": extracted.phone,
        "gender": extracted.gender,
        "date_of_birth": extracted.date_of_birth,
        "marital_status": extracted.marital_status,
        "father_name": extracted.father_name,
        "mother_name": extracted.mother_name,
        "known_languages": extracted.known_languages,
        "candidate_address": extracted.candidate_address,
        "pincode_postal_code": extracted.pincode_postal_code,
        "hobbies": extracted.hobbies,
        "training":[t.model_dump() for t in extracted.training],
        "linkedin_url": extracted.linkedin_url,
        "other_urls": extracted.other_urls,
        "education": [e.model_dump() for e in extracted.education],
        "experience": [e.model_dump() for e in extracted.experience],
        "skills": extracted.skills,
        "certifications": extracted.certifications,
        "internships": [e.model_dump() for e in extracted.internships],
        "summary": extracted.profile_summary,
        "needs_review": needs_review,
        "extraction_method": extraction_method,
        "parse_score": score.total_score,
        "ocr_deep_dive_used": ocr_deep_dive_used,
    }