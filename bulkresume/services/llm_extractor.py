import itertools
import json
import logging
import os
import threading
import time
import traceback

from django.conf import settings
from groq import Groq
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from .regex_fallback import regex_extract_basic_fields
from .schemas import JobDescriptionExtraction, ResumeExtraction, ResumeJDMatchResult

logger = logging.getLogger('bulkresume')

print("GROQ KEYS LOADED:", len(settings.GROQ_API_KEYS))
print("HTTPS_PROXY:", os.environ.get("HTTPS_PROXY"))
print("HTTP_PROXY:", os.environ.get("HTTP_PROXY"))

# ── Multiple Groq clients, round-robin ─────────────────────────────────────
# Temporary measure while the company sets up a paid/higher-tier Groq plan.
# All keys belong to company-owned accounts.
_clients = [Groq(api_key=key) for key in settings.GROQ_API_KEYS]
if not _clients:
    raise RuntimeError("No GROQ_API_KEYS configured in settings/.env")

_client_lock = threading.Lock()
_client_cycle = itertools.cycle(range(len(_clients)))

# ── Per-key token usage tracker (this worker session only) ─────────────────
_usage_lock = threading.Lock()
_key_usage = {
    idx: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for idx in range(len(_clients))
}


def _record_usage(key_idx: int, usage) -> None:
    """Accumulate token usage for a given key index and print a running summary."""
    if usage is None:
        return
    with _usage_lock:
        stats = _key_usage[key_idx]
        stats["requests"] += 1
        stats["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        stats["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        stats["total_tokens"] += getattr(usage, "total_tokens", 0) or 0

    _print_usage_summary()


def _print_usage_summary() -> None:
    """Log a per-key token usage table (this session's cumulative usage)."""
    with _usage_lock:
        lines = []
        lines.append("\n" + "=" * 70)
        lines.append(f"{'Key':<6}{'Requests':<12}{'Prompt Tok':<14}{'Completion Tok':<16}{'Total Tok':<12}")
        lines.append("-" * 70)
        grand_total = 0
        for idx, stats in _key_usage.items():
            lines.append(
                f"{idx:<6}{stats['requests']:<12}{stats['prompt_tokens']:<14}"
                f"{stats['completion_tokens']:<16}{stats['total_tokens']:<12}"
            )
            grand_total += stats["total_tokens"]
        lines.append("-" * 70)
        lines.append(f"{'TOTAL':<6}{'':<12}{'':<14}{'':<16}{grand_total:<12}")
        lines.append("=" * 70 + "\n")
        logger.info("\n".join(lines))


def get_usage_summary() -> dict:
    """Programmatic access to current session usage stats."""
    with _usage_lock:
        return {idx: dict(stats) for idx, stats in _key_usage.items()}


def _get_next_client() -> tuple[Groq, int]:
    """Thread-safe round-robin client selection. Returns (client, index)."""
    with _client_lock:
        idx = next(_client_cycle)
    return _clients[idx], idx


def _make_schema_strict(schema: dict) -> dict:
    """Recursively force `required` to include every property. Kept for
    reference / future use, but NOT applied currently — see STRICT_CAPABLE_MODELS
    below for why strict mode is disabled."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            schema["required"] = list(schema["properties"].keys())
        for value in schema.values():
            if isinstance(value, dict):
                _make_schema_strict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _make_schema_strict(item)
    return schema


RESUME_JSON_SCHEMA = ResumeExtraction.model_json_schema()

EXTRACTION_PROMPT = """You are a resume parsing engine. Extract structured information from the resume text below and return valid JSON with these fields:
name, email, phone, gender, date_of_birth, marital_status, father_name, mother_name, linkedin_url, other_urls, education, known_languages, candidate_address, pincode/postal_code, hobbies, training, experience, skills, certifications, internships, profile_summary.

Rules:
- Include every field above, even if empty ("" or []). Never omit a field.
- education: list each degree/qualification with degree, institution, and year if available.
- hobbies: extract as a list of short keywords/phrases (e.g., "Reading", "Cricket"). If not mentioned, return [].
- training: list each training/workshop with name, provider/institution, and year if available. If not mentioned, return [].
- profile_summary: if the resume has an existing summary/objective section, copy it verbatim. Otherwise write a brief 2-3 sentence summary.
- experience/internships descriptions: summarize in 1-2 short sentences, keeping specific numbers, tools, and achievements. Avoid long paragraphs.
Resume text:
---
{resume_text}
---
"""

# Strict mode disabled: with strict=True, Groq forces every schema property
# to be `required`, and if the model (openai/gpt-oss-20b) truncates or skips
# even one field (common on longer resumes), Groq rejects the ENTIRE response
# with a 400 json_validate_failed error — even though the rest of the JSON
# was perfectly usable. Pydantic (ResumeExtraction) already fills in safe
# defaults ("" / []) for any field the model omits, so we don't need Groq's
# strict enforcement — it was causing avoidable failures, not preventing them.
STRICT_CAPABLE_MODELS = ()

# Small stagger between calls even with multiple keys — avoids all keys
# bursting Groq at the exact same instant, and gives headroom under each
# individual key's per-key TPM/RPM/TPD cap.
MIN_SECONDS_BETWEEN_CALLS = 1.0


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _call_groq(resume_text: str, model: str) -> dict:
    time.sleep(MIN_SECONDS_BETWEEN_CALLS)

    client, key_idx = _get_next_client()
    is_strict = model in STRICT_CAPABLE_MODELS

    try:
        # with_raw_response gives access to HTTP headers (live rate-limit
        # info straight from Groq) in addition to the parsed response body.
        raw_response = client.chat.completions.with_raw_response.create(
            model=model,
            messages=[
                {"role": "system", "content": "You extract structured resume data as valid JSON."},
                {"role": "user", "content": EXTRACTION_PROMPT.format(resume_text=resume_text[:6000])},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "resume_extraction",
                    "schema": RESUME_JSON_SCHEMA,
                    "strict": is_strict,
                },
            },
            temperature=0.1,
        )

        headers = raw_response.headers
        remaining_requests = headers.get('x-ratelimit-remaining-requests')
        remaining_tokens = headers.get('x-ratelimit-remaining-tokens')
        limit_requests = headers.get('x-ratelimit-limit-requests')
        limit_tokens = headers.get('x-ratelimit-limit-tokens')

        logger.info(
            f"Key {key_idx} | Live remaining (this window): "
            f"{remaining_requests}/{limit_requests} requests, "
            f"{remaining_tokens}/{limit_tokens} tokens"
        )

        response = raw_response.parse()
        _record_usage(key_idx, getattr(response, "usage", None))

        return json.loads(response.choices[0].message.content)
    except Exception as e:
        status_code = getattr(e, 'status_code', None)
        response_body = getattr(e, 'body', None) or getattr(e, 'message', None)
        logger.warning(
            f"Groq call failed on key index {key_idx} | "
            f"type={type(e).__name__} | status={status_code} | detail={response_body or e}"
        )
        raise


def extract_structured_data(resume_text: str) -> tuple[ResumeExtraction, bool]:
    """Returns (extracted_data, needs_review)."""
    model = settings.GROQ_MODEL
    try:
        raw_json = _call_groq(resume_text, model)
        validated = ResumeExtraction(**raw_json)
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Groq extraction failed, falling back to regex: {exc}")
        logger.warning(traceback.format_exc())
        fallback_data = regex_extract_basic_fields(resume_text)
        return ResumeExtraction(**fallback_data), True


# ── JD extraction (new) ─────────────────────────────────────────────────────
# Separate prompt/schema/call function rather than reusing _call_groq's resume
# prompt — same reasoning single_parse.py used for not touching pipeline.py's
# tested update_or_create call: keep the already-working resume-extraction
# path completely untouched. Shares the client pool / rate-limit / retry
# infra above (_get_next_client, _record_usage, MIN_SECONDS_BETWEEN_CALLS).

JD_JSON_SCHEMA = JobDescriptionExtraction.model_json_schema()

JD_EXTRACTION_PROMPT = """You are a job-description parsing engine. Extract structured information from the job description text below and return valid JSON with these fields:
job_title, must_have_skills, good_to_have_skills, min_experience_years, max_experience_years, qualifications, responsibilities, location, employment_type, other_requirements.

Rules:
- Include every field above, even if empty ("" or []). Never omit a field.
- must_have_skills: skills/technologies explicitly stated as required/mandatory. Keep each as a short keyword/phrase (e.g., "React", "5 years Python").
- good_to_have_skills: skills explicitly called out as preferred/nice-to-have/bonus. If the JD doesn't distinguish must-have from nice-to-have, put all listed skills under must_have_skills and leave this empty.
- min_experience_years / max_experience_years: plain numbers as strings (e.g. "3", "5"). Leave "" if not mentioned or if it's a range with no upper bound.
- qualifications: required education/certifications (e.g. "B.Tech in Computer Science", "PMP certification").
- responsibilities: key day-to-day duties, as short bullet-style phrases.
- other_requirements: anything else explicitly required (e.g. "willing to relocate", "night shift").
Job description text:
---
{jd_text}
---
"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _call_groq_jd(jd_text: str, model: str) -> dict:
    time.sleep(MIN_SECONDS_BETWEEN_CALLS)

    client, key_idx = _get_next_client()

    try:
        raw_response = client.chat.completions.with_raw_response.create(
            model=model,
            messages=[
                {"role": "system", "content": "You extract structured job-description data as valid JSON."},
                {"role": "user", "content": JD_EXTRACTION_PROMPT.format(jd_text=jd_text[:6000])},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "jd_extraction",
                    "schema": JD_JSON_SCHEMA,
                    "strict": False,
                },
            },
            temperature=0.1,
        )
        response = raw_response.parse()
        _record_usage(key_idx, getattr(response, "usage", None))
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        status_code = getattr(e, 'status_code', None)
        response_body = getattr(e, 'body', None) or getattr(e, 'message', None)
        logger.warning(
            f"Groq JD-extraction call failed on key index {key_idx} | "
            f"type={type(e).__name__} | status={status_code} | detail={response_body or e}"
        )
        raise


def extract_jd_structured(jd_text: str) -> tuple[JobDescriptionExtraction, bool]:
    """Returns (structured_jd, needs_review). needs_review=True means the LLM
    call failed and every field came back empty — callers should flag the
    batch, since matching every resume against a blank JD is meaningless."""
    model = settings.GROQ_MODEL
    try:
        raw_json = _call_groq_jd(jd_text, model)
        validated = JobDescriptionExtraction(**raw_json)
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Groq JD extraction failed: {exc}")
        logger.warning(traceback.format_exc())
        return JobDescriptionExtraction(), True


# ── Resume <-> JD matching (new) ────────────────────────────────────────────

MATCH_JSON_SCHEMA = ResumeJDMatchResult.model_json_schema()

MATCH_PROMPT = """You are an expert technical recruiter. Compare the CANDIDATE profile against the JOB DESCRIPTION below, field by field, and return valid JSON scoring how well the candidate matches.

Rules:
- overall_match_percent: a single 0-100 score for how well this candidate matches the JD overall. Weigh must-have skills and required experience heavily; weigh good-to-have skills and qualifications lightly.
- matched_skills / missing_skills: compare candidate skills + experience descriptions against must_have_skills and good_to_have_skills. A skill counts as matched if it's present, a clear synonym (e.g. "ReactJS" matches "React"), or reasonably implied by the candidate's experience descriptions — not only exact string matches. List every JD skill under either matched_skills or missing_skills, never both, never omitted.
- matched_qualifications / missing_qualifications: same idea for the JD's qualifications list.
- experience_match: one short sentence comparing the candidate's total relevant experience against min_experience_years/max_experience_years (e.g. "Meets requirement: 5 years vs 3+ required").
- field_breakdown: one entry per JD requirement (skills, qualifications, experience, location if specified) with field, jd_requirement, candidate_value (what the candidate actually has, or "" if nothing), matched (true/false), match_percent (0-100 for that one line item), and a one-sentence note.
- strengths_summary: 2-3 sentences on what the candidate matches well.
- gaps_summary: 2-3 sentences on what's missing or weak. If nothing is missing, say so plainly.
- recommendation: one of "Strong Match", "Partial Match", or "Weak Match".
- Do not invent candidate details that aren't present in the candidate profile below. Missing/unclear information should count against the match, not be assumed favorably.

Job Description (structured JSON):
---
{jd_json}
---
Candidate Profile (structured JSON):
---
{candidate_json}
---
"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _call_groq_match(jd_json: str, candidate_json: str, model: str) -> dict:
    time.sleep(MIN_SECONDS_BETWEEN_CALLS)

    client, key_idx = _get_next_client()

    try:
        raw_response = client.chat.completions.with_raw_response.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise technical recruiter that compares candidates to job "
                               "descriptions and returns valid JSON. Never omit a required field.",
                },
                {
                    "role": "user",
                    "content": MATCH_PROMPT.format(
                        jd_json=jd_json[:6000], candidate_json=candidate_json[:6000]
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "resume_jd_match",
                    "schema": MATCH_JSON_SCHEMA,
                    "strict": False,
                },
            },
            temperature=0.1,
        )
        response = raw_response.parse()
        _record_usage(key_idx, getattr(response, "usage", None))
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        status_code = getattr(e, 'status_code', None)
        response_body = getattr(e, 'body', None) or getattr(e, 'message', None)
        logger.warning(
            f"Groq match call failed on key index {key_idx} | "
            f"type={type(e).__name__} | status={status_code} | detail={response_body or e}"
        )
        raise


def match_resume_to_jd(jd_data: dict, candidate_data: dict) -> tuple[ResumeJDMatchResult, bool]:
    """Returns (match_result, needs_review). On LLM failure, needs_review=True
    and the caller (services/matcher.py) falls back to the deterministic
    skill-overlap matcher so a Groq outage degrades match quality instead of
    breaking the endpoint outright."""
    model = settings.GROQ_MODEL
    try:
        raw_json = _call_groq_match(json.dumps(jd_data), json.dumps(candidate_data), model)
        validated = ResumeJDMatchResult(**raw_json)
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Groq resume-JD match failed: {exc}")
        logger.warning(traceback.format_exc())
        return ResumeJDMatchResult(), True
