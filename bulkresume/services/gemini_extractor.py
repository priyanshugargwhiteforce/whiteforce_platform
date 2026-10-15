"""
GEMINI-BASED RESUME EXTRACTION
--------------------------------
Used ONLY by services/single_parse.py (i.e. only the synchronous
SingleResumeParseView / POST /api/resumes/parse/ endpoint). The bulk
upload pipeline (services/pipeline.py) and the JD-match pipeline
(services/match_pipeline.py) continue to use Groq via
services/llm_extractor.extract_structured_data() -- completely untouched.

Implemented as a plain REST call via `requests` (already a project
dependency) rather than adding google-generativeai/google-genai as a new
SDK -- keeps requirements.txt untouched.

API-key rotation: the HTTP call goes through services/gemini_keys.py, which
holds all GEMINI_API_KEY_n keys and automatically falls over to the next key
when one hits its rate limit / quota (429).

Uses Gemini's responseMimeType=application/json JSON mode (prompt-only,
NOT the responseSchema structured-output feature) -- Gemini's schema
format is an OpenAPI-3.0 subset that doesn't support Pydantic's $defs/$ref
nested-model output (needed here for Education/Experience/Training), so
translating RESUME_JSON_SCHEMA the way llm_extractor.py does for Groq
would require a from-scratch schema rewrite. Prompting for the exact JSON
shape + validating with the SAME ResumeExtraction Pydantic model already
used everywhere else gets the identical safety net (missing/invalid
fields raise ValidationError, exactly like the Groq path) with far less
risk.
"""
import json
import logging
import threading
import traceback

from django.conf import settings
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from .gemini_keys import get_gemini_pool
from .json_coerce import TOP_LEVEL_ALIASES, coerce_to_model
from .regex_fallback import regex_extract_basic_fields
from .schemas import ResumeExtraction

logger = logging.getLogger('bulkresume')

GEMINI_MODEL = getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite')

# ── Usage tracker (this process's session only) ─────────────────────────────
# Same idea as llm_extractor.py's per-key Groq usage table. Totals are across
# all Gemini keys combined; the per-call log line shows which key was used.
_usage_lock = threading.Lock()
_gemini_usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _record_usage(usage_metadata: dict | None) -> None:
    """Accumulate token usage from Gemini's usageMetadata block and print a
    running summary, mirroring llm_extractor.py's _record_usage/_print_usage_summary."""
    if not usage_metadata:
        return
    with _usage_lock:
        _gemini_usage["requests"] += 1
        _gemini_usage["prompt_tokens"] += usage_metadata.get("promptTokenCount", 0) or 0
        _gemini_usage["completion_tokens"] += usage_metadata.get("candidatesTokenCount", 0) or 0
        _gemini_usage["total_tokens"] += usage_metadata.get("totalTokenCount", 0) or 0

    _print_usage_summary()


def _print_usage_summary() -> None:
    with _usage_lock:
        stats = dict(_gemini_usage)
    logger.info(
        "\n" + "=" * 70 + "\n"
        f"GEMINI ({GEMINI_MODEL}) | Requests: {stats['requests']} | "
        f"Prompt Tok: {stats['prompt_tokens']} | "
        f"Completion Tok: {stats['completion_tokens']} | "
        f"Total Tok: {stats['total_tokens']}\n"
        + "=" * 70
    )


def get_usage_summary() -> dict:
    """Programmatic access to current session usage stats."""
    with _usage_lock:
        return dict(_gemini_usage)


# Field list/instructions mirror llm_extractor.EXTRACTION_PROMPT (the Groq
# path) exactly, but with each field's expected JSON shape spelled out
# explicitly -- since there's no schema enforcement here, the model needs
# unambiguous shape instructions in the prompt itself. Kept as a SEPARATE
# prompt (not imported/shared with llm_extractor.py) so a future prompt
# tweak on one provider never silently changes the other's behavior.
GEMINI_EXTRACTION_PROMPT = """You are a resume parsing engine. Extract structured information from the resume text below and return ONLY a valid JSON object (no markdown, no code fences, no commentary) with exactly these fields:
name, email, phone, gender, date_of_birth, marital_status, father_name, mother_name, linkedin_url, other_urls, education, known_languages, candidate_address, total_experience, pincode_postal_code, hobbies, training, experience, projects, skills, certifications, internships, profile_summary.

Field shapes:
- name, email, phone, gender, date_of_birth, marital_status, father_name, mother_name, linkedin_url, candidate_address, total_experience, pincode_postal_code, profile_summary: plain strings ("" if unknown).
- other_urls, known_languages, hobbies, skills, certifications: plain lists of strings ([] if none).
- education: list of objects {{"degree": str, "institution": str, "year": str}}.
- training: list of objects {{"name": str, "provider": str, "year": str}}.
- experience, internships: list of objects {{"title": str, "company": str, "duration": str}}.
- projects: list of objects {{"title": str, "company": str, "start_date": str, "end_date": str, "description": str, "methodologies": [str]}}.

Rules:
- Include every field above, even if empty ("" or []). Never omit a field.
- hobbies: short keywords/phrases (e.g., "Reading", "Cricket"). If not mentioned, return [].
- total_experience: a single string (e.g., "5 years", "3.5 years"). If not mentioned, return "".
- training: list each training/workshop with name, provider/institution, and year if available. If not mentioned, return [].
- profile_summary: if the resume has an existing summary/objective section, copy it verbatim. Otherwise write a brief 2-3 sentence summary.
- projects: list each project with title, company (client/employer the project was done for, "" if not stated), start_date and end_date (as written in the resume, e.g. "Jan 2022", "Present"; "" if not stated), a 1-2 sentence description, and the methodologies used. If not mentioned, return [].

Resume text:
---
{resume_text}
---
"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def _call_gemini(resume_text: str) -> dict:
    payload = {
        "contents": [
            {"parts": [{"text": GEMINI_EXTRACTION_PROMPT.format(resume_text=resume_text[:6000])}]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    # The pool tries key #1 first and automatically falls over to #2, #3 on
    # 429 / quota errors. Raises RuntimeError if no key is configured and
    # GeminiKeysUnavailable if every key is cooling down.
    response, key_no = get_gemini_pool().post(payload, model=GEMINI_MODEL, timeout=30, label="extract")

    if response.status_code != 200:
        logger.warning(f"Gemini call failed | status={response.status_code} | body={response.text[:500]}")
        response.raise_for_status()

    data = response.json()

    usage = data.get("usageMetadata")
    _record_usage(usage)
    if usage:
        logger.info(
            f"Gemini ({GEMINI_MODEL}) key #{key_no} | this call: "
            f"{usage.get('promptTokenCount', 0)} prompt + "
            f"{usage.get('candidatesTokenCount', 0)} completion = "
            f"{usage.get('totalTokenCount', 0)} total tokens"
        )

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc

    return json.loads(text)


def extract_structured_data_gemini(resume_text: str) -> tuple[ResumeExtraction, bool]:
    """
    Gemini equivalent of llm_extractor.extract_structured_data() -- same
    signature, same (ResumeExtraction, needs_review) return shape, same
    regex-fallback-on-failure behavior -- so services/single_parse.py can
    swap one call for the other with no other code changes.
    """
    try:
        raw_json = _call_gemini(resume_text)
        # coerce_to_model drops any key the schema no longer has (e.g. a stray
        # experience "description"), instead of failing the whole parse.
        validated = ResumeExtraction(**coerce_to_model(raw_json, ResumeExtraction, TOP_LEVEL_ALIASES))
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Gemini extraction failed, falling back to regex: {exc}")
        logger.warning(traceback.format_exc())
        fallback_data = regex_extract_basic_fields(resume_text)
        return ResumeExtraction(**fallback_data), True