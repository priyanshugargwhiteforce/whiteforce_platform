"""
Per-resume JD-matching pipeline. Called by tasks.match_resume_task (Celery),
one call per resume in a match batch. Mirrors services/pipeline.py's
process_resume() shape (status transitions, error handling) but for the
resume<->JD matching flow:

  1. Classify the file, hash it.
  2. Run the full single-resume parse (LLM extraction + OCR deep-dive
     retry, services/single_parse.py) — every resume gets parsed fresh,
     no DB profile lookup/shortcut.
  3. Match the resulting candidate profile against the JD's structured
     fields (services/matcher.py — LLM match with deterministic fallback).
  4. Save everything onto the ResumeMatch row.

Deliberately does NOT touch services/pipeline.py or services/single_parse.py
— reuses their already-tested functions instead of forking them, same
approach single_parse.py itself used relative to pipeline.py.
"""
import hashlib
import logging

from ..models import ResumeMatch
from .file_classifier import classify_file
from .matcher import match_candidate_against_jd
from .single_parse import build_profile_defaults, parse_single_resume

logger = logging.getLogger('bulkresume')


def process_resume_match(resume_match_id: int) -> None:
    match = ResumeMatch.objects.select_related('resume', 'jd').get(id=resume_match_id)
    resume = match.resume
    jd = match.jd
    log_prefix = f"ResumeMatch#{match.id}: "

    match.status = 'processing'
    match.save(update_fields=['status'])
    resume.status = 'processing'
    resume.save(update_fields=['status'])

    try:
        file_path = resume.file.path

        with open(file_path, 'rb') as f:
            resume.file_hash = hashlib.sha256(f.read()).hexdigest()

        file_type = classify_file(file_path)
        resume.file_type = file_type
        resume.save(update_fields=['file_type', 'file_hash'])

        # ── Parse the resume (no DB lookup shortcut — every resume goes
        # through the full parse). parse_single_resume() does its own
        # extract_text() + clean_resume_text() internally, so there's no
        # separate text-extraction step here. ──────────────────────────────
        parsed = parse_single_resume(file_path, file_type, log_prefix=log_prefix)
        candidate_profile = build_profile_defaults(
            parsed["extracted"], parsed["needs_review"], parsed["extraction_method"],
            parsed["score"], parsed["ocr_deep_dive_used"],
        )
        match.source = 'parsed'
        match.extraction_method = candidate_profile["extraction_method"]
        match.parse_score = candidate_profile["parse_score"]
        match.phone = candidate_profile.get("phone") or ""
        resume.raw_text = parsed["raw_text"]
        resume.save(update_fields=['raw_text'])

        match.candidate_profile = candidate_profile
        match.candidate_name = candidate_profile.get("name") or ""

        # ── Match candidate profile against the JD ──────────────────────────
        result = match_candidate_against_jd(jd.parsed, candidate_profile, log_prefix=log_prefix)

        match.match_percent = result.get("overall_match_percent", 0.0)
        match.match_method = result.get("match_method", "")
        match.matched_skills = result.get("matched_skills", [])
        match.missing_skills = result.get("missing_skills", [])
        match.matched_qualifications = result.get("matched_qualifications", [])
        match.missing_qualifications = result.get("missing_qualifications", [])
        match.experience_match = result.get("experience_match", "")
        match.field_breakdown = result.get("field_breakdown", [])
        match.strengths_summary = result.get("strengths_summary", "")
        match.gaps_summary = result.get("gaps_summary", "")
        match.recommendation = result.get("recommendation", "")

        match.status = 'done'
        match.save()

        resume.status = 'done'
        resume.save(update_fields=['status'])

        logger.info(f"{log_prefix}done — {match.match_percent}% match ({match.source}/{match.match_method})")

    except Exception as exc:
        match.status = 'failed'
        match.error_message = str(exc)
        match.save(update_fields=['status', 'error_message'])
        resume.status = 'failed'
        resume.error_message = str(exc)
        resume.save(update_fields=['status', 'error_message'])
        logger.error(f"{log_prefix}failed: {exc}")
        raise