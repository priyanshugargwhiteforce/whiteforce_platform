"""
Per-resume pipeline for the Gemini JD-match route (POST /api/resumes/match-gemini/).

Same status transitions and result fields as services/match_pipeline.py, so
the existing status endpoint reads its results unchanged; the difference is
that the resume is parsed by Gemini into match_schemas.MatchResumeExtraction
(services/gemini_resume_parser.py) instead of by the bulk Groq parser.
The JD is structured by Gemini as well (services/jd_extractor.py), and the
match itself is the same verified Gemini match (services/matcher.py).
"""
import hashlib
import logging

from ..models import ResumeMatch
from .file_classifier import classify_file
from .gemini_resume_parser import build_match_profile, parse_resume_gemini_route
from .matcher import match_candidate_against_jd

logger = logging.getLogger('bulkresume')


def process_resume_match_gemini(resume_match_id: int) -> None:
    match = ResumeMatch.objects.select_related('resume', 'jd').get(id=resume_match_id)
    resume = match.resume
    jd = match.jd
    log_prefix = f"ResumeMatch#{match.id} (gemini): "

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

        # Every resume is parsed fresh -- no duplicate check, no DB profile shortcut.
        parsed = parse_resume_gemini_route(file_path, file_type, log_prefix=log_prefix)
        candidate_profile = build_match_profile(parsed)

        match.source = 'parsed'
        match.extraction_method = candidate_profile["extraction_method"]
        match.parse_score = candidate_profile["parse_score"]
        match.phone = candidate_profile.get("phone") or ""
        match.candidate_profile = candidate_profile
        match.candidate_name = candidate_profile.get("name") or ""
        resume.raw_text = parsed["raw_text"]
        resume.save(update_fields=['raw_text'])

        result = match_candidate_against_jd(
            jd.parsed, candidate_profile, log_prefix=log_prefix, raw_text=parsed["raw_text"]
        )

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
