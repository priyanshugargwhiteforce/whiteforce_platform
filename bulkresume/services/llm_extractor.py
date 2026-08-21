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
name, email, phone, linkedin_url, other_urls, education, experience, skills, certifications, internships, profile_summary.

Rules:
- Include every field above, even if empty ("" or []). Never omit a field.
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