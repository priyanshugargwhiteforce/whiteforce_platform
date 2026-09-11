import json
import logging

from .extractors import extract_text
from .file_classifier import classify_file
from .llm_extractor import extract_jd_structured
from .pipeline import clean_resume_text  # generic whitespace cleanup, not resume-specific despite the name
from .schemas import JobDescriptionExtraction

logger = logging.getLogger('bulkresume')


class JDExtractionError(Exception):
    """Raised for a malformed jd_json payload — a 400, not a 500."""
    pass


def extract_jd_from_file(file_path: str) -> tuple[str, dict]:
    """Same text-extraction pipeline as a resume (pdf/docx/doc/image), then
    LLM-structures it into JobDescriptionExtraction fields.
    Returns (raw_text, structured_dict)."""
    file_type = classify_file(file_path)
    raw_text = extract_text(file_path, file_type)
    raw_text = clean_resume_text(raw_text)
    structured, needs_review = extract_jd_structured(raw_text)
    if needs_review:
        logger.warning("JD LLM extraction failed for uploaded JD file — structured fields will be empty")
    return raw_text, structured.model_dump()


def extract_jd_from_text(jd_text: str) -> tuple[str, dict]:
    """Pasted/plain-text JD -> LLM-structured fields.
    Returns (raw_text, structured_dict)."""
    jd_text = clean_resume_text(jd_text)
    structured, needs_review = extract_jd_structured(jd_text)
    if needs_review:
        logger.warning("JD LLM extraction failed for pasted JD text — structured fields will be empty")
    return jd_text, structured.model_dump()


def extract_jd_from_json(jd_json) -> tuple[str, dict]:
    """
    Accepts either an already-parsed dict or a raw JSON string.

    If the JSON already uses our structured field names (job_title,
    must_have_skills, ...), we map it directly onto JobDescriptionExtraction
    without an LLM call — it's already structured, no need to re-derive it.
    If the JSON is some other shape (e.g. {"description": "..."} or a raw
    JD blob from another ATS), we fall back to treating its text content as
    plain JD text and run it through the LLM extractor instead.

    Returns (raw_text_or_json_string, structured_dict).
    """
    if isinstance(jd_json, (str, bytes)):
        try:
            jd_json = json.loads(jd_json)
        except (ValueError, TypeError) as exc:
            raise JDExtractionError(f"jd_json is not valid JSON: {exc}") from exc

    if not isinstance(jd_json, dict):
        raise JDExtractionError("jd_json must be a JSON object")

    recognized_keys = {
        "job_title", "must_have_skills", "good_to_have_skills",
        "min_experience_years", "max_experience_years", "qualifications",
        "responsibilities", "location", "employment_type", "other_requirements",
    }
    if recognized_keys & jd_json.keys():
        structured = JobDescriptionExtraction(
            **{k: v for k, v in jd_json.items() if k in recognized_keys}
        )
        return json.dumps(jd_json), structured.model_dump()

    # Unrecognized shape — best-effort: pull out a text-ish field if present,
    # otherwise stringify the whole payload, and LLM-parse it as free text.
    jd_text = jd_json.get("description") or jd_json.get("text") or jd_json.get("jd") or json.dumps(jd_json)
    return extract_jd_from_text(jd_text)
