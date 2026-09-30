"""
RESUME PARSE FOR THE JD-MATCH FLOW (bulk route)
-------------------------------------------------
Parses one resume the same way the bulk pipeline does -- Groq via
llm_extractor.extract_structured_data(), score, OCR deep-dive retry when the
score is low -- but returns the result in memory (no ParsedProfile row, no
duplicate check) so match_pipeline can match it against the JD right away.

Only difference from the bulk pipeline: if Groq's extraction falls back to
regex (rate limit, bad JSON), Gemini is tried once before accepting the
regex result. A match computed from an empty regex profile is meaningless
("0% match" for a good candidate), so a second real LLM attempt is worth the
extra call, and it only happens on failures.

Same return shape as single_parse.parse_single_resume(), so
single_parse.build_profile_defaults() works on the result unchanged.
services/single_parse.py itself is not modified.
"""
import logging

from .deep_dive_ocr import deep_dive_ocr_extract
from .extractors import extract_text
from .gemini_extractor import extract_structured_data_gemini
from .llm_extractor import extract_structured_data as extract_structured_data_groq
from .parse_score import compute_parse_score
from .pipeline import OCR_DEEP_DIVE_SCORE_THRESHOLD, clean_resume_text

logger = logging.getLogger('bulkresume')


def _extract(text: str, log_prefix: str):
    """Returns (extracted, needs_review, used_gemini_fallback)."""
    extracted, needs_review = extract_structured_data_groq(text)
    if needs_review:
        logger.info(f"{log_prefix}Groq extraction fell back to regex, retrying with Gemini")
        gemini_extracted, gemini_review = extract_structured_data_gemini(text)
        if not gemini_review:
            return gemini_extracted, False, True
    return extracted, needs_review, False


def _method(needs_review: bool, used_gemini: bool, deep_dive: bool) -> str:
    if needs_review:
        return "regex_fallback"
    if used_gemini:
        return "llm_gemini_ocr_deep_dive" if deep_dive else "llm_gemini_fallback"
    return "llm_ocr_deep_dive" if deep_dive else "llm"


def parse_resume_bulk_route(file_path: str, file_type: str, log_prefix: str = "") -> dict:
    raw_text = clean_resume_text(extract_text(file_path, file_type))

    extracted, needs_review, used_gemini = _extract(raw_text, log_prefix)
    extraction_method = _method(needs_review, used_gemini, deep_dive=False)

    score = compute_parse_score(extracted, raw_text)
    ocr_deep_dive_used = False

    # A regex-fallback result is an LLM failure, not a bad-text problem: OCR
    # would only re-send the same text to the same failing LLM. Only OCR when
    # the LLM worked but the parse still scored low (garbled/sparse text).
    if not needs_review and score.needs_ocr_deep_dive(threshold=OCR_DEEP_DIVE_SCORE_THRESHOLD):
        logger.info(
            f"{log_prefix}parse score {score.total_score} <= "
            f"{OCR_DEEP_DIVE_SCORE_THRESHOLD}, attempting OCR deep-dive"
        )
        try:
            ocr_text = deep_dive_ocr_extract(file_path, file_type)
            if ocr_text:
                ocr_text = clean_resume_text(ocr_text)
                ocr_extracted, ocr_review, ocr_gemini = _extract(ocr_text, log_prefix)
                ocr_score = compute_parse_score(ocr_extracted, ocr_text)
                logger.info(
                    f"{log_prefix}deep-dive score {ocr_score.total_score} vs original {score.total_score}"
                )
                if not ocr_review and ocr_score.total_score > score.total_score:
                    extracted, needs_review, used_gemini = ocr_extracted, ocr_review, ocr_gemini
                    extraction_method = _method(needs_review, used_gemini, deep_dive=True)
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
