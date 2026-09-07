import hashlib
import logging
import re

from django.conf import settings

from ..models import ParsedProfile, Resume
from .deep_dive_ocr import deep_dive_ocr_extract
from .dedup_check import DedupCheckError, check_duplicate_in_db
from .extractors import extract_text
from .file_classifier import classify_file
from .llm_extractor import extract_structured_data
from .parse_score import compute_parse_score
from .regex_fallback import regex_extract_basic_fields

logger = logging.getLogger('bulkresume')

# Score <= this triggers the OCR deep-dive retry. Override via
# settings.OCR_DEEP_DIVE_SCORE_THRESHOLD if you want to tune it without
# touching this file; defaults to 55 if that setting isn't set.
OCR_DEEP_DIVE_SCORE_THRESHOLD = getattr(settings, 'OCR_DEEP_DIVE_SCORE_THRESHOLD', 55.0)


def clean_resume_text(text: str) -> str:
    """Strip redundant whitespace that wastes tokens without adding signal."""
    # Multiple blank lines -> single blank line
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Multiple spaces/tabs -> single space
    text = re.sub(r'[ \t]{2,}', ' ', text)
    # Trailing whitespace per line
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    return text.strip()


def process_resume(resume_id: int) -> None:
    resume = Resume.objects.get(id=resume_id)
    resume.status = 'processing'
    resume.save(update_fields=['status'])

    try:
        file_path = resume.file.path

        with open(file_path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
        resume.file_hash = file_hash

        file_type = classify_file(file_path)
        resume.file_type = file_type

        raw_text = extract_text(file_path, file_type)
        raw_text = clean_resume_text(raw_text)  # trim token-wasting whitespace
        resume.raw_text = raw_text
        resume.save(update_fields=['file_type', 'raw_text', 'file_hash'])

        # ── Cheap pre-check: pull email/phone with regex before touching the LLM ──
        quick_fields = regex_extract_basic_fields(raw_text)
        quick_email = quick_fields.get("email", "")
        quick_phone = quick_fields.get("phone", "")

        is_duplicate = False
        try:
            is_duplicate = check_duplicate_in_db(quick_email, quick_phone)
        except DedupCheckError:
            # API failed (network/5xx/etc.) — fail OPEN so we don't silently
            # drop resumes just because the dedup API had a hiccup.
            logger.warning(f"Resume#{resume.id}: dedup check failed, proceeding with LLM parse")
            is_duplicate = False

        if is_duplicate:
            # Candidate (by email and/or phone) already exists in DB —
            # skip the LLM entirely, don't create a ParsedProfile.
            resume.is_duplicate = True
            resume.status = 'duplicate'
            resume.error_message = 'Resume already parsed'
            resume.save(update_fields=['status', 'is_duplicate', 'error_message'])
            logger.info(f"Resume#{resume.id} skipped: duplicate email/phone found in DB")
            return

        extracted, needs_review = extract_structured_data(raw_text)
        extraction_method = "regex_fallback" if needs_review else "llm"

        score = compute_parse_score(extracted, raw_text)
        ocr_deep_dive_used = False

        # ── Deep-dive: only when the parse actually looks bad ──────────────
        # Score <= threshold means either the LLM couldn't fill the key
        # fields, or the source text itself looked garbled/sparse — both
        # point at a bad text extraction, not a bad LLM call. Re-OCR with
        # stronger preprocessing and re-parse; keep whichever attempt scored
        # higher. Wrapped so a deep-dive failure never breaks the main flow —
        # worst case we just keep the original (already-saved) result.
        if score.needs_ocr_deep_dive(threshold=OCR_DEEP_DIVE_SCORE_THRESHOLD):
            logger.info(
                f"Resume#{resume.id}: parse score {score.total_score} <= "
                f"{OCR_DEEP_DIVE_SCORE_THRESHOLD}, attempting OCR deep-dive"
            )
            try:
                ocr_text = deep_dive_ocr_extract(file_path, file_type)
                if ocr_text:
                    ocr_text = clean_resume_text(ocr_text)
                    ocr_extracted, ocr_needs_review = extract_structured_data(ocr_text)
                    ocr_score = compute_parse_score(ocr_extracted, ocr_text)

                    logger.info(
                        f"Resume#{resume.id}: deep-dive score {ocr_score.total_score} "
                        f"vs original {score.total_score}"
                    )

                    if ocr_score.total_score > score.total_score:
                        extracted = ocr_extracted
                        needs_review = ocr_needs_review
                        extraction_method = "regex_fallback" if needs_review else "llm_ocr_deep_dive"
                        raw_text = ocr_text
                        score = ocr_score
                        ocr_deep_dive_used = True
                        resume.raw_text = raw_text
                        resume.save(update_fields=['raw_text'])
                else:
                    logger.info(f"Resume#{resume.id}: deep-dive OCR produced no usable text, keeping original")
            except Exception as exc:
                # Never let a deep-dive failure take down an otherwise-successful parse.
                logger.warning(f"Resume#{resume.id}: OCR deep-dive raised {exc!r}, keeping original result")

        ParsedProfile.objects.update_or_create(
            resume=resume,
            defaults={
                "name": extracted.name,
                "email": extracted.email,
                "phone": extracted.phone,
                "gender": extracted.gender,
                "date_of_birth": extracted.date_of_birth,
                "marital_status": extracted.marital_status,
                "father_name": extracted.father_name,
                "mother_name": extracted.mother_name,
                "known_languages": extracted.known_languages,
                "candidate_address": extracted.candidate_address,
                "pincode_postal_code": extracted.pincode_postal_code,
                "hobbies": extracted.hobbies,
                "training": extracted.training,
                "linkedin_url": extracted.linkedin_url,
                "other_urls": extracted.other_urls,
                "education": [e.model_dump() for e in extracted.education],
                "experience": [e.model_dump() for e in extracted.experience],
                "skills": extracted.skills,
                "certifications": extracted.certifications,
                "internships": [e.model_dump() for e in extracted.internships],
                "summary": extracted.profile_summary,
                "needs_review": needs_review,
                "extraction_method": extraction_method,
                "parse_score": score.total_score,
                "ocr_deep_dive_used": ocr_deep_dive_used,
            },
        )

        resume.status = 'done'
        resume.save(update_fields=['status'])
        logger.info(f"Resume#{resume.id} processed successfully")

    except Exception as exc:
        resume.status = 'failed'
        resume.error_message = str(exc)
        resume.save(update_fields=['status', 'error_message'])
        logger.error(f"Resume#{resume.id} failed: {exc}")
        raise