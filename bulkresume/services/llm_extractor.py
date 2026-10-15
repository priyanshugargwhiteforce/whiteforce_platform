import itertools
import json
import logging
import os
import re
import threading
import time
import traceback

from django.conf import settings
from groq import Groq
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from .regex_fallback import regex_extract_basic_fields
from .experience_calc import estimate_total_experience
from .grounding import ground_extraction
from .json_coerce import TOP_LEVEL_ALIASES, coerce_to_model
from .schemas import ResumeExtraction

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

# ── Per-key rate-limit skip tracker ────────────────────────────────────────
# When a key returns 429, we record how long to skip it so subsequent retries
# jump straight to the next available key instead of hammering the same one.
_rate_limit_lock = threading.Lock()
_rate_limited_until: dict[int, float] = {idx: 0.0 for idx in range(len(_clients))}


def _mark_rate_limited(key_idx: int, retry_after_seconds: float = 300.0) -> None:
    """Mark a key as rate-limited for the given number of seconds."""
    with _rate_limit_lock:
        _rate_limited_until[key_idx] = time.time() + retry_after_seconds
    logger.warning(f"Key {key_idx} marked rate-limited for {retry_after_seconds:.0f}s")


def _get_next_available_client() -> tuple[Groq, int] | None:
    """Round-robin but skip keys that are currently rate-limited.
    Tries every key once; returns None if all are exhausted."""
    now = time.time()
    for _ in range(len(_clients)):
        with _client_lock:
            idx = next(_client_cycle)
        with _rate_limit_lock:
            if _rate_limited_until[idx] <= now:
                return _clients[idx], idx
    return None  # all keys rate-limited


def _parse_retry_seconds(error_body: str) -> float:
    """Parse 'Please try again in Xm Y.Zs' from Groq error message."""
    match = re.search(r'try again in (\d+)m([\d.]+)s', error_body)
    if match:
        return int(match.group(1)) * 60 + float(match.group(2))
    # fallback: look for just seconds
    match = re.search(r'try again in ([\d.]+)s', error_body)
    if match:
        return float(match.group(1))
    return 300.0  # default 5 min


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

# ── Shared Groq call plumbing ────────────────────────────────────────────────
#
# Reasoning effort for gpt-oss models. Reasoning tokens are billed and counted
# against TPM and add seconds of latency, but resume/JD extraction and
# requirement-by-requirement matching are mostly reading + copying, not
# multi-step reasoning. "low" cuts latency and token use noticeably, and also
# removes a cause of the empty-output `json_validate_failed` 400s (reasoning
# eating the completion budget before any JSON is written). Override with
# settings.GROQ_REASONING_EFFORT ("low" | "medium" | "high").
GROQ_REASONING_EFFORT = getattr(settings, 'GROQ_REASONING_EFFORT', 'low')

# How much of a resume's text is sent to the bulk parser. The old 6000-char
# cap silently dropped education/skills/certifications on longer resumes
# (they usually sit at the end). Override with settings.BULK_RESUME_MAX_CHARS.
RESUME_MAX_CHARS = getattr(settings, 'BULK_RESUME_MAX_CHARS', 7500)


def _reasoning_kwargs(model: str) -> dict:
    """`reasoning_effort` is only understood by the gpt-oss family; sending it
    to any other Groq model would be a 400, so it's added conditionally."""
    if GROQ_REASONING_EFFORT and 'gpt-oss' in (model or ''):
        return {"extra_body": {"reasoning_effort": GROQ_REASONING_EFFORT}}
    return {}


@retry(stop=stop_after_attempt(len(_clients)), wait=wait_exponential(multiplier=1, min=1, max=10))
def _groq_json_call(system_prompt: str, user_prompt: str, schema_name: str, schema: dict,
                    model: str, stagger: float, label: str) -> dict:
    """One Groq JSON-schema call with key rotation + 429 tracking. The static
    instructions go in the SYSTEM message and only the variable text
    (resume / JD / candidate) in the user message, so the long shared prefix
    is identical on every call and eligible for Groq's prompt caching."""
    if stagger:
        time.sleep(stagger)

    result = _get_next_available_client()
    if result is None:
        raise RuntimeError("All Groq keys are currently rate-limited")
    client, key_idx = result

    try:
        # with_raw_response gives access to HTTP headers (live rate-limit
        # info straight from Groq) in addition to the parsed response body.
        raw_response = client.chat.completions.with_raw_response.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema,
                    # Strict mode stays off: with strict=True Groq rejects the
                    # WHOLE response if the model skips even one field. Missing
                    # fields are filled by Pydantic defaults instead.
                    "strict": False,
                },
            },
            temperature=0.1,
            **_reasoning_kwargs(model),
        )

        headers = raw_response.headers
        logger.info(
            f"Key {key_idx} | {label} | Live remaining (this window): "
            f"{headers.get('x-ratelimit-remaining-requests')}/{headers.get('x-ratelimit-limit-requests')} requests, "
            f"{headers.get('x-ratelimit-remaining-tokens')}/{headers.get('x-ratelimit-limit-tokens')} tokens"
        )

        response = raw_response.parse()
        _record_usage(key_idx, getattr(response, "usage", None))

        return json.loads(response.choices[0].message.content)

    except Exception as e:
        if getattr(e, 'status_code', None) == 429:
            retry_secs = _parse_retry_seconds(str(getattr(e, 'body', '') or ''))
            _mark_rate_limited(key_idx, retry_secs)
        status_code = getattr(e, 'status_code', None)
        response_body = getattr(e, 'body', None) or getattr(e, 'message', None)
        logger.warning(
            f"Groq {label} call failed on key index {key_idx} | "
            f"type={type(e).__name__} | status={status_code} | detail={response_body or e}"
        )
        raise


# ── Resume extraction (bulk pipeline) ────────────────────────────────────────

RESUME_SYSTEM_PROMPT = """You are a resume-parsing engine. Convert the resume text sent by the user into ONE JSON object. The text may come from PDF/DOCX/OCR, so lines can be out of order, split across columns or table cells, or contain stray characters.

OUTPUT: JSON only, with exactly these keys, all present, no other keys, never null ("" for unknown text, [] for unknown lists):
name, email, phone, gender, date_of_birth, marital_status, father_name, mother_name, linkedin_url, other_urls, education, known_languages, candidate_address, total_experience, pincode_postal_code, hobbies, training, experience, projects, skills, certifications, internships, profile_summary

SHAPES
- strings: name, email, phone, gender, date_of_birth, marital_status, father_name, mother_name, linkedin_url, candidate_address, total_experience, pincode_postal_code, profile_summary
- lists of strings: other_urls, known_languages, hobbies, skills, certifications
- education: [{"degree": str, "institution": str, "year": str, "percentage": str or null}]
- training: [{"name": str, "provider": str, "year": str}]
- experience, internships: [{"title": str, "company": str, "duration": str}]
- projects: [{"title": str, "company": str, "start_date": str, "end_date": str, "description": str, "methodologies": [str]}]
Every value inside these objects is a string, except methodologies (list of strings).

NO-HALLUCINATION RULES
1. Extract only what the resume explicitly states. Never guess, infer, or fill gaps from general knowledge: do not infer gender from a name, invent dates, employers, degrees, skills, links, phone numbers or emails. Not written = empty.
2. Copy names, companies, institutions, titles, dates, numbers and URLs exactly as written (original spelling and casing). Do not translate, correct or reformat them.
3. Put each fact in one right field. Do not duplicate a role in both experience and internships, and do not move content from one section into another to avoid an empty field.
4. Do not drop anything. List EVERY item of every section (each job, project, degree, table row, course, certification, language, hobby, link) even on long resumes. Keep the detail inside each item (tools, numbers, board, percentage, client, dates).

FIELD RULES
- name: the candidate's own name (usually the top line). Never a referee, company or institution.
- email, phone: the candidate's own. If several phone numbers, use the first as written (keep any +91).
- date_of_birth: as written. gender: only if written (Male/Female or a Gender label). marital_status: as written.
- candidate_address: the full address as one string. pincode_postal_code: only the PIN/ZIP code that appears in the address or is labelled as such.
- linkedin_url: the LinkedIn link. other_urls: every other link (GitHub, portfolio, etc.).
- known_languages: spoken/written languages only (one per item, split "English, Hindi"), not programming languages.
- education: one object per qualification (school, diploma, degree, post-graduation, doctorate), every row of a table separately, in the resume's order.
  * Each of the three values must be text you can point to in the resume, on the same line/row/block as that qualification. If a value is not written there, use "". Never fill it from another qualification, from the experience section, or from what is "typical".
  * degree = the qualification name exactly as written ("B.Tech", "HSC", "Diploma in Mechanical Engineering"), plus in brackets ONLY the stream/specialisation, category/sector and board written beside it, e.g. "ITI (Fitter) (NCVT)", "B.E. (Computer Science)", "HSC (Maharashtra State Board)". Marks never go in degree (see percentage). Do not expand abbreviations ("B.Tech" stays "B.Tech") and do not add a stream that is not written.
  * institution = the school/college/university/institute name only, exactly as written, and never repeated inside degree. If only a board or university is named and no institute, put it in institution only when the resume names it as the place of study; otherwise leave "".
  * year = the passing year, or the start-end period, exactly as written for that qualification; "" if none is written next to it. Never use a year taken from a job, project or date of birth.
  * percentage = the marks/percentage/CGPA/grade for that qualification exactly as written, keeping its unit or scale ("83.78%", "58.33 %", "8.4 CGPA", "First Class"). Use null (not "") when the resume gives no marks for that qualification. Each qualification gets its own value; never copy one row's marks to another, never calculate or convert (do not turn CGPA into a percentage), and never take marks from a job or project.
  * A degree with no institution, or an institution with no degree, is still one entry; keep what is written and leave the rest "" (percentage null).
- experience: one object per job/role held at an employer, in the resume's order (include current and past roles, part-time, freelance and contract).
  * title = the designation/role for that job. company = the employer/organisation. Look for them in the job's heading line, in the line just above or below the dates, in "Role:", "Designation:", "Company:", "Client:", "Worked as" style labels, and in sentences like "Worked as X at Y" / "Y, as X". Fill both whenever the resume gives them, even if the layout is a table, a sidebar, or the words are on separate lines. Leave one "" only when it is truly not written anywhere for that job.
  * duration = the period or length exactly as written for that job ("Jan 2021 - Present", "2 years"); "" if none.
  * Only title, company and duration are extracted for a job (no description). If the resume names only a company or only a role for a job, still create the entry with what is written.
- internships: internship/apprentice/trainee positions at an organisation, same shape and same rules as experience. Courses with a provider belong in training instead.
- training: workshops, courses and programmes attended (name, provider, year).
- projects: one object per project that the resume itself presents as a project (a Projects / Academic Projects / Personal Projects section, or a named project inside a job). Never turn job responsibilities, skills, tools or internship duties into projects; if the resume has no projects, return []. title = the project name exactly as written. company = client/employer only if stated. start_date/end_date as written (e.g. "Jan 2022", "Present"), else "". description: 1-2 sentences on what was built or done and the outcome. methodologies: the methods, frameworks, technologies and tools named for that project.
- skills: individual skills, tools and technologies as short items, taken from skills sections plus tools explicitly named in experience/projects. Split lists into single items, no duplicates.
- certifications: certification/licence names only (with issuer/year if in the same line). Do not put degrees here.
- hobbies: short keywords, from Hobbies/Interests sections only.
- total_experience: only a total that the resume itself states (e.g. "4 years", "3+ years of experience"). If the resume does not state one, return "" -- do NOT add up durations yourself (that is computed afterwards).
- profile_summary: if the resume has a summary/objective/profile section, copy it verbatim. Otherwise write 2 sentences using only facts from the resume."""

RESUME_USER_TEMPLATE = "Resume text:\n---\n{resume_text}\n---"


def _call_groq(resume_text: str, model: str) -> dict:
    return _groq_json_call(
        RESUME_SYSTEM_PROMPT,
        RESUME_USER_TEMPLATE.format(resume_text=resume_text[:RESUME_MAX_CHARS]),
        "resume_extraction", RESUME_JSON_SCHEMA, model,
        stagger=MIN_SECONDS_BETWEEN_CALLS, label="resume",
    )


# Small stagger between bulk-parse calls even with multiple keys — avoids all
# keys bursting Groq at the exact same instant, and gives headroom under each
# individual key's per-key TPM/RPM/TPD cap. Only the bulk resume parser uses
# it; JD extraction and matching are user-waiting calls and don't sleep.
MIN_SECONDS_BETWEEN_CALLS = getattr(settings, 'GROQ_MIN_SECONDS_BETWEEN_CALLS', 0.3)


def extract_structured_data(resume_text: str) -> tuple[ResumeExtraction, bool]:
    """Returns (extracted_data, needs_review)."""
    model = settings.GROQ_MODEL
    try:
        raw_json = _call_groq(resume_text, model)
        validated = ResumeExtraction(**coerce_to_model(raw_json, ResumeExtraction, TOP_LEVEL_ALIASES))
        # Blank any education/job value that isn't actually in the resume text.
        validated = ground_extraction(validated, resume_text)
        if not validated.total_experience:
            # Not stated on the resume: sum the written job durations in code
            # (same resume -> same number every run, unlike LLM arithmetic).
            validated.total_experience = estimate_total_experience(
                [e.duration for e in validated.experience]
            )
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Groq extraction failed, falling back to regex: {exc}")
        logger.warning(traceback.format_exc())
        fallback_data = regex_extract_basic_fields(resume_text)
        return ResumeExtraction(**fallback_data), True
