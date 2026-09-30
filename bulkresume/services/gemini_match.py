"""
GEMINI-BASED JD EXTRACTION + RESUME<->JD MATCHING
---------------------------------------------------
The JD-match flow parses each resume through the normal bulk route (Groq,
services/llm_extractor.py); the two LLM steps that are specific to matching
-- structuring the JD, and comparing one candidate to it -- run on Gemini
and live here. Single-parse (services/gemini_extractor.py) is untouched.

Why the shape of this module:
  - One JD is compared against ~10-15 resumes in a batch, so every resume
    must be judged by the SAME rubric (temperature 0, fixed row order,
    fixed `field` values) or scores across candidates aren't comparable.
  - The static rubric + the JD go in the system instruction and only the
    candidate goes in the user message, so all calls in a batch share the
    same prefix.
  - Anything the model says is verified afterwards against the actual resume
    (services/match_verify.py), so a hallucinated "match" can't survive.
"""
import json
import logging
import re
import traceback

import requests
from django.conf import settings
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from .json_coerce import coerce_to_model
from .schemas import JobDescriptionExtraction, ResumeJDMatchResult

logger = logging.getLogger('bulkresume')

GEMINI_API_KEY = getattr(settings, 'GEMINI_API_KEY', '')
GEMINI_MODEL = getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite')
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT_SECONDS = 60


class _GeminiRetryable(Exception):
    """429 / 5xx / unparseable output -- worth another attempt."""


@retry(
    retry=retry_if_exception_type(_GeminiRetryable),
    stop=stop_after_attempt(4),
    # Jittered: ~15 match tasks hit Gemini at once; without jitter every one
    # that gets a 429 retries at the same instant and collides again.
    wait=wait_random_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _call_gemini_json(system_prompt: str, user_prompt: str, label: str) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured in settings/.env")

    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(
            GEMINI_API_URL.format(model=GEMINI_MODEL),
            params={"key": GEMINI_API_KEY}, json=payload, timeout=GEMINI_TIMEOUT_SECONDS,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _GeminiRetryable(f"{label}: network error {exc!r}") from exc

    if response.status_code == 429 or response.status_code >= 500:
        logger.warning(f"Gemini {label} call retryable | status={response.status_code} | body={response.text[:300]}")
        raise _GeminiRetryable(f"{label}: HTTP {response.status_code}")
    if response.status_code != 200:
        logger.warning(f"Gemini {label} call failed | status={response.status_code} | body={response.text[:500]}")
        response.raise_for_status()

    data = response.json()
    usage = data.get("usageMetadata") or {}
    logger.info(
        f"Gemini ({GEMINI_MODEL}) {label} | {usage.get('promptTokenCount', 0)} prompt + "
        f"{usage.get('candidatesTokenCount', 0)} completion tokens"
    )

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    except (KeyError, IndexError, TypeError) as exc:
        raise _GeminiRetryable(f"{label}: unexpected response shape: {str(data)[:300]}") from exc

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text).strip()
    try:
        return json.loads(text)
    except ValueError as exc:
        raise _GeminiRetryable(f"{label}: model returned invalid JSON: {exc}") from exc


# ── JD extraction ─────────────────────────────────────────────────────────────

JD_SYSTEM_PROMPT = """You are a job-description parsing engine. Convert the job description sent by the user into ONE JSON object.

OUTPUT: JSON only, exactly these keys, all present, no other keys, never null ("" for unknown text, [] for unknown lists):
job_title, must_have_skills, good_to_have_skills, min_experience_years, max_experience_years, qualifications, responsibilities, location, employment_type, other_requirements
Strings: job_title, min_experience_years, max_experience_years, location, employment_type. Lists of strings: all the others.

RULES
- Extract only what the JD states. Never invent requirements, numbers or skills. Not written = empty. Copy wording as written; do not paraphrase a requirement.
- job_title / location / employment_type: as written (e.g. "Full-time", "Contract", "Hybrid - Pune").
- must_have_skills: skills, tools, technologies and domain knowledge that are required/mandatory/"must"/"proficient in"/"strong experience in", or listed under Requirements/Skills without a "preferred" qualifier. One short item each ("React", "PostgreSQL", "REST APIs"): split "React, Node.js and AWS" into three items. Keep an attached duration in the item ("5 years Python"). Do not list soft-skill filler ("team player") unless the JD lists it as a requirement.
- good_to_have_skills: only what the JD calls preferred / nice-to-have / bonus / plus / desirable. If the JD does not separate them, put every skill in must_have_skills and leave this empty.
- min_experience_years / max_experience_years: plain numbers as strings. "3-5 years" -> "3" and "5"; "5+ years" or "minimum 5" -> "5" and ""; "up to 4 years" -> "" and "4"; "fresher"/"0-1 years" -> "0" and "1". Use the overall experience requirement, not per-skill years. "" if not stated.
- qualifications: required education and certifications as written ("B.E./B.Tech in Computer Science", "PMP"). Keep alternatives ("ITI or Diploma", "B.E. or MCA") together as ONE item, because meeting either one satisfies the requirement. Likewise keep "A or B" skill alternatives ("React or Angular") as one item.
- responsibilities: key duties as short phrases, one per item.
- other_requirements: anything else explicitly required that fits nowhere above (notice period, relocation, shift, language, travel, work authorisation).
- Do not repeat the same item in several lists."""

JD_USER_TEMPLATE = "Job description text:\n---\n{jd_text}\n---"


def extract_jd_structured(jd_text: str) -> tuple[JobDescriptionExtraction, bool]:
    """Returns (structured_jd, needs_review). needs_review=True means the LLM
    call failed and every field came back empty -- callers should flag the
    batch, since matching every resume against a blank JD is meaningless."""
    try:
        raw_json = _call_gemini_json(JD_SYSTEM_PROMPT, JD_USER_TEMPLATE.format(jd_text=jd_text[:10000]), "JD")
        validated = JobDescriptionExtraction(**coerce_to_model(raw_json, JobDescriptionExtraction))
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Gemini JD extraction failed: {exc}")
        logger.warning(traceback.format_exc())
        return JobDescriptionExtraction(), True


# ── Resume <-> JD matching ───────────────────────────────────────────────────
#
# matcher.reconcile_match_result() recomputes the overall percent, the
# matched/missing lists and the recommendation FROM field_breakdown, so the
# model is only asked for what is actually used: the per-requirement rows and
# a little prose. Fewer output tokens = lower latency per resume, and no
# chance of the model's totals contradicting its own rows.

MATCH_SYSTEM_PROMPT = """You are a precise technical recruiter. Compare ONE candidate with the JOB requirements and return ONE JSON object with exactly these keys: field_breakdown, experience_match, strengths_summary, gaps_summary. Nothing else; totals are computed elsewhere.

The JOB (JSON) is given at the end of these instructions. The CANDIDATE (JSON) is sent by the user.

field_breakdown: one row for EVERY requirement, in this order: each item of must_have_skills, each item of good_to_have_skills, each item of qualifications, one "experience" row (only if min/max experience is given), one "location" row (only if location is given). Never skip, merge, reorder or add rows. Row shape:
{"field": "must_have_skill" | "good_to_have_skill" | "qualification" | "experience" | "location",
 "jd_requirement": the requirement copied EXACTLY, character for character, from the JOB JSON (for experience use e.g. "2-4 years", for location the job's location),
 "candidate_value": EVIDENCE - a short snippet copied VERBATIM from the candidate JSON (a skill entry, a role/company/description phrase, a degree). Join up to 3 snippets with " | ". "" if there is no evidence.
 "matched": true/false,
 "match_percent": integer 0-100,
 "note": at most 12 words}

EVIDENCE RULE (most important): every match must be backed by text that really appears in the candidate JSON. Do not paraphrase evidence, do not use general knowledge about what a job title "usually" involves, and never assume a skill from a company name or a degree name. If you cannot quote evidence, the row is match_percent 0, matched false, candidate_value "".

SCORING (identical for every candidate, so scores stay comparable):
- Skills: 100 = the skill is listed in skills or clearly used in experience/projects/internships text. 70-90 = a clear synonym or direct variant (ReactJS = React, Postgres = PostgreSQL, JS = JavaScript). 30-55 = related but different skill, or a duty that only hints at the skill without naming it (evidence quoted; this is NOT a match). 0 = no evidence. Absence of evidence is 0, not a guess.
- Qualification: 100 if the candidate holds it (or ANY one of the alternatives when it says "A or B") or a higher/equivalent one in a relevant field; 30-55 if only a related field or lower level; 0 otherwise.
- Experience: compare the candidate's total_experience (or the sum of the durations in their experience list) with the JOB min/max. Meets or exceeds min -> 100. Below min -> candidate years / min years x 100. Not determinable -> 0.
- Location: 100 if the candidate's address is in the same city/area as the job location or the job is remote; 0 if elsewhere or unknown.
- matched = true only if match_percent >= 60.

experience_match: one short sentence, e.g. "Meets requirement: 5 years vs 3+ required".
strengths_summary: 1-2 sentences naming the specific matched skills/roles. gaps_summary: 1-2 sentences naming the specific missing requirements; if nothing is missing, say so."""

MATCH_USER_TEMPLATE = "CANDIDATE (JSON):\n{candidate_json}"

# Only what matching actually uses. Contact details, DOB, parents' names,
# hobbies, languages, URLs and parse metadata are noise for the comparison
# (and personal data that never needs to go to the LLM).
_JD_MATCH_KEYS = ("job_title", "must_have_skills", "good_to_have_skills",
                  "min_experience_years", "max_experience_years", "qualifications", "location")
CANDIDATE_MATCH_KEYS = ("total_experience", "skills", "experience", "internships", "projects",
                        "education", "certifications", "training", "candidate_address",
                        "summary", "profile_summary")


def compact_json(data: dict, keys: tuple) -> str:
    slim = {k: data[k] for k in keys if data.get(k) not in (None, "", [], {})}
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def match_resume_to_jd(jd_data: dict, candidate_data: dict) -> tuple[ResumeJDMatchResult, bool]:
    """Returns (match_result, needs_review). On LLM failure -- or a response
    with no field_breakdown, which would otherwise reconcile to a bogus 0% --
    needs_review=True and the caller (services/matcher.py) falls back to the
    deterministic skill-overlap matcher, so a Gemini problem degrades match
    quality instead of breaking the endpoint outright."""
    try:
        system_prompt = MATCH_SYSTEM_PROMPT + "\n\nJOB (JSON):\n" + compact_json(jd_data, _JD_MATCH_KEYS)[:6000]
        raw_json = _call_gemini_json(
            system_prompt,
            MATCH_USER_TEMPLATE.format(candidate_json=compact_json(candidate_data, CANDIDATE_MATCH_KEYS)[:12000]),
            "match",
        )
        validated = ResumeJDMatchResult(**coerce_to_model(raw_json, ResumeJDMatchResult))
        if not validated.field_breakdown:
            raise ValueError("match response had no field_breakdown rows")
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Gemini resume-JD match failed: {exc}")
        logger.warning(traceback.format_exc())
        return ResumeJDMatchResult(), True
