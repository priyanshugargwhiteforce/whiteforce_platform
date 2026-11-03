"""
CLAUDE RESUME EXTRACTOR
------------------------
Parses one resume with Claude (settings.CLAUDE_MODEL, default Claude Haiku 5.5)
into the same ResumeExtraction schema and with the same prompt the Groq bulk
parser uses, so results look identical whichever model produced them.

Used by:
  - pipeline.py: bulk upload fallback when every Groq key fails / is rate-limited
  - single_parse.py: POST /api/resumes/parse-claude/

Key: ANTHROPIC_API_KEY in .env (one key shared by both).
"""
import json
import logging
import threading
import traceback

import anthropic
from django.conf import settings

from .experience_calc import estimate_total_experience
from .grounding import ground_extraction
from .json_coerce import TOP_LEVEL_ALIASES, coerce_to_model
from .llm_extractor import RESUME_SYSTEM_PROMPT, RESUME_USER_TEMPLATE, _make_schema_strict
from .regex_fallback import regex_extract_basic_fields
from .schemas import ResumeExtraction

logger = logging.getLogger('bulkresume')

CLAUDE_MODEL = getattr(settings, 'CLAUDE_MODEL', 'claude-haiku-5-5')
# Resume extraction is mostly reading + copying, so "low" keeps latency and
# token spend down (same reasoning as GROQ_REASONING_EFFORT).
CLAUDE_EFFORT = getattr(settings, 'CLAUDE_EFFORT', 'low')

# Every ResumeExtraction field has a default, so none is "required" and the
# API rejects the schema as too complex (39 optional properties). Marking all
# of them required fixes that; the prompt already says to send "" / [] for
# anything missing, so no information is lost.
RESUME_CLAUDE_SCHEMA = _make_schema_strict(
    anthropic.transform_schema(ResumeExtraction.model_json_schema())
)

_client = None
_client_lock = threading.Lock()


def _get_client() -> anthropic.Anthropic:
    """Created on first use, so a missing ANTHROPIC_API_KEY only fails the
    Claude call (-> regex fallback) instead of crashing Django at import."""
    global _client
    with _client_lock:
        if _client is None:
            api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured in settings/.env")
            # Organization-level keys (not scoped to a workspace) must say
            # which workspace to use, or every request is a 400.
            workspace_id = getattr(settings, 'ANTHROPIC_WORKSPACE_ID', '')
            headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
            _client = anthropic.Anthropic(api_key=api_key, default_headers=headers)
        return _client


def extract_structured_data_claude(resume_text: str) -> tuple[ResumeExtraction, bool]:
    """Returns (extracted_data, needs_review); same contract as
    llm_extractor.extract_structured_data()."""
    try:
        response = _get_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system=RESUME_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": RESUME_USER_TEMPLATE.format(resume_text=resume_text)}],
            output_config={
                "effort": CLAUDE_EFFORT,
                "format": {"type": "json_schema", "schema": RESUME_CLAUDE_SCHEMA},
            },
        )
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"Claude returned no usable output (stop_reason={response.stop_reason})")
        raw_json = json.loads(next(b.text for b in response.content if b.type == "text"))

        usage = response.usage
        logger.info(
            f"Claude resume | {CLAUDE_MODEL} | input={usage.input_tokens} output={usage.output_tokens} "
            f"| request_id={response._request_id}"
        )

        # Blank any education/job value that isn't actually in the resume text.
        validated = ResumeExtraction(**coerce_to_model(raw_json, ResumeExtraction, TOP_LEVEL_ALIASES))
        validated = ground_extraction(validated, resume_text)
        if not validated.total_experience:
            # Not stated on the resume: sum the written job durations in code
            # (same resume -> same number every run, unlike LLM arithmetic).
            validated.total_experience = estimate_total_experience(
                [e.duration for e in validated.experience]
            )
        return validated, False
    except Exception as exc:
        logger.warning(f"Claude extraction failed, falling back to regex: {exc!r}")
        logger.warning(traceback.format_exc())
        fallback_data = regex_extract_basic_fields(resume_text)
        return ResumeExtraction(**fallback_data), True
