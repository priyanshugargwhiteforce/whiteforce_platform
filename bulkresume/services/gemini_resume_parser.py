"""
GEMINI RESUME PARSER FOR THE JD-MATCH ROUTE
---------------------------------------------
Parses one resume with Gemini into match_schemas.MatchResumeExtraction, then
scores it and (if the score is low) retries once on OCR text -- the same
shape of flow as the bulk and single-parse pipelines, but on its own schema
and its own prompt so it can't disturb either of them.

Used only by services/match_pipeline_gemini.py (POST /api/resumes/match-gemini/).
"""
import logging
import traceback

from pydantic import ValidationError

from .deep_dive_ocr import deep_dive_ocr_extract
from .experience_calc import estimate_total_experience
from .extractors import extract_text
from .gemini_match import _call_gemini_json
from .grounding import ground_extraction
from .json_coerce import coerce_to_model
from .match_schemas import MatchResumeExtraction
from .parse_score import compute_parse_score
from .pipeline import OCR_DEEP_DIVE_SCORE_THRESHOLD, clean_resume_text
from .regex_fallback import regex_extract_basic_fields

logger = logging.getLogger('bulkresume')

# Gemini's context is large and matching benefits from the whole resume, so
# this is much higher than the Groq bulk parser's cap.
RESUME_MAX_CHARS = 14000

RESUME_SYSTEM_PROMPT = """You are a resume-parsing engine. Convert the resume text sent by the user into ONE JSON object. The text may come from PDF/DOCX/OCR, so lines can be out of order, split across columns or table cells, or contain stray characters.

OUTPUT: JSON only, with exactly these keys, all present, no other keys, never null except where stated ("" for unknown text, [] for unknown lists):
name, email, phone, linkedin_url, other_urls, candidate_address, known_languages, total_experience, education, experience, internships, projects, skills, certifications, training, profile_summary

SHAPES
- strings: name, email, phone, linkedin_url, candidate_address, total_experience, profile_summary
- lists of strings: other_urls, known_languages, skills, certifications
- education: [{"degree": str, "institution": str, "year": str, "percentage": str or null}]
- experience, internships: [{"title": str, "company": str, "duration": str, "description": str}]
- projects: [{"title": str, "company": str, "start_date": str, "end_date": str, "description": str, "methodologies": [str]}]
- training: [{"name": str, "provider": str, "year": str}]
Every value inside these objects is a string, except methodologies (list of strings) and percentage (string or null).

NO-HALLUCINATION RULES
1. Extract only what the resume explicitly states. Never guess, infer, or fill gaps from general knowledge: do not invent dates, employers, degrees, skills, links, phone numbers or emails. Not written = empty.
2. Copy names, companies, institutions, titles, dates, numbers and URLs exactly as written (original spelling and casing). Do not translate, correct, expand or reformat them.
3. Put each fact in one right field. Do not duplicate a role in both experience and internships.
4. Do not drop anything. List EVERY item of every section (each job, project, degree, table row, course, certification) even on long resumes, keeping the detail inside each item.

FIELD RULES
- name: the candidate's own name (usually the top line). Never a referee, company or institution.
- email, phone: the candidate's own; if several phone numbers, the first as written (keep any +91).
- candidate_address: the full address as one string. linkedin_url: the LinkedIn link. other_urls: every other link.
- known_languages: spoken/written languages only, one per item, not programming languages.
- education: one object per qualification, every table row separately, in the resume's order. Each value must be text written on the same line/row/block as that qualification; if not written there, use "" (percentage null). Never fill a value from another row or section.
  * degree = the qualification exactly as written ("B.Tech", "HSC"), plus in brackets ONLY the stream/specialisation, category/sector and board written beside it. Do not expand abbreviations. Marks never go in degree.
  * institution = the school/college/university/institute name only, exactly as written.
  * year = the passing year or start-end period as written for that qualification, else "".
  * percentage = the marks/percentage/CGPA/grade for that qualification exactly as written, with its unit ("83.78%", "8.4 CGPA"); null if not written. Never copy one row's marks to another, never convert CGPA to a percentage.
- experience: one object per job/role held at an employer, in the resume's order (current and past, part-time, freelance, contract).
  * title = the designation/role; company = the employer. Look for them in the job's heading line, the lines just above/below the dates, and labels like "Role:", "Designation:", "Company:", "Client:", "Worked as X at Y". Fill both whenever the resume gives them, even in tables or multi-line layouts; leave one "" only if it is truly not written for that job.
  * duration = the period or length exactly as written for that job, else "".
  * description = a complete but compact account of that role built only from the lines written under it: every distinct responsibility, project or achievement, the tools/technologies used, and any numbers or percentages. 2-4 sentences (up to about 70 words); merge bullets instead of dropping them. Never add a duty, tool or number that is not written. If only a company and one short line are given, still create the entry and copy that line into description.
- internships: internship/apprentice/trainee positions, same shape and rules as experience. Courses with a provider belong in training.
- projects: one object per project. company = client/employer only if stated. start_date/end_date as written, else "". description: 1-2 sentences on what was built or done and the outcome. methodologies: methods, frameworks, technologies and tools named for that project.
- skills: individual skills, tools and technologies as short items, taken from skills sections plus tools explicitly named in experience/projects. Split lists into single items, no duplicates.
- certifications: certification/licence names only (with issuer/year if in the same line). Do not put degrees here.
- training: workshops, courses and programmes attended (name, provider, year).
- total_experience: only a total that the resume itself states (e.g. "4 years"). If none is stated return "" -- do not add up durations yourself.
- profile_summary: copy the resume's summary/objective/profile section verbatim if present; otherwise write 2 sentences using only facts from the resume."""

RESUME_USER_TEMPLATE = "Resume text:\n---\n{resume_text}\n---"


def extract_resume_gemini(resume_text: str) -> tuple[MatchResumeExtraction, bool]:
    """Returns (extracted, needs_review); needs_review=True means Gemini failed
    and only regex-level fields (email/phone/links) were recovered."""
    try:
        raw_json = _call_gemini_json(
            RESUME_SYSTEM_PROMPT,
            RESUME_USER_TEMPLATE.format(resume_text=resume_text[:RESUME_MAX_CHARS]),
            "resume",
        )
        validated = MatchResumeExtraction(**coerce_to_model(raw_json, MatchResumeExtraction))
        validated = ground_extraction(validated, resume_text)
        if not validated.total_experience:
            # Not stated on the resume: sum the written job durations in code
            # (same resume -> same number every run, unlike LLM arithmetic).
            validated.total_experience = estimate_total_experience([j.duration for j in validated.experience])
        return validated, False
    except (ValidationError, Exception) as exc:
        logger.warning(f"Gemini resume extraction failed, falling back to regex: {exc}")
        logger.warning(traceback.format_exc())
        fallback = regex_extract_basic_fields(resume_text)
        allowed = MatchResumeExtraction.model_fields
        return MatchResumeExtraction(**{k: v for k, v in fallback.items() if k in allowed}), True


def _method(needs_review: bool, deep_dive: bool) -> str:
    if needs_review:
        return "regex_fallback"
    return "llm_gemini_ocr_deep_dive" if deep_dive else "llm_gemini"


def parse_resume_gemini_route(file_path: str, file_type: str, log_prefix: str = "") -> dict:
    """Same return shape as bulk_route_parse.parse_resume_bulk_route()."""
    raw_text = clean_resume_text(extract_text(file_path, file_type))

    extracted, needs_review = extract_resume_gemini(raw_text)
    extraction_method = _method(needs_review, deep_dive=False)
    score = compute_parse_score(extracted, raw_text)
    ocr_deep_dive_used = False

    # A regex fallback means the LLM call failed, not that the text is bad;
    # OCR would resend the same text to the same failing call. Only OCR when
    # the LLM worked and the parse still scored low (garbled/sparse text).
    if not needs_review and score.needs_ocr_deep_dive(threshold=OCR_DEEP_DIVE_SCORE_THRESHOLD):
        logger.info(f"{log_prefix}parse score {score.total_score} <= {OCR_DEEP_DIVE_SCORE_THRESHOLD}, attempting OCR deep-dive")
        try:
            ocr_text = deep_dive_ocr_extract(file_path, file_type)
            if ocr_text:
                ocr_text = clean_resume_text(ocr_text)
                ocr_extracted, ocr_review = extract_resume_gemini(ocr_text)
                ocr_score = compute_parse_score(ocr_extracted, ocr_text)
                logger.info(f"{log_prefix}deep-dive score {ocr_score.total_score} vs original {score.total_score}")
                if not ocr_review and ocr_score.total_score > score.total_score:
                    extracted, needs_review = ocr_extracted, ocr_review
                    extraction_method = _method(needs_review, deep_dive=True)
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


def build_match_profile(parsed: dict) -> dict:
    """Candidate profile dict saved on ResumeMatch.candidate_profile and fed to
    the matcher. `summary` mirrors profile_summary for API compatibility with
    the other match route."""
    profile = parsed["extracted"].model_dump()
    profile["summary"] = profile.get("profile_summary", "")
    profile.update({
        "needs_review": parsed["needs_review"],
        "extraction_method": parsed["extraction_method"],
        "parse_score": parsed["score"].total_score,
        "ocr_deep_dive_used": parsed["ocr_deep_dive_used"],
    })
    return profile
