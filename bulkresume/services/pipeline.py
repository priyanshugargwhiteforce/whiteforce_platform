import hashlib
import logging
import re

from ..models import ParsedProfile, Resume
from .extractors import extract_text
from .file_classifier import classify_file
from .llm_extractor import extract_structured_data

logger = logging.getLogger('bulkresume')


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

        extracted, needs_review = extract_structured_data(raw_text)

        ParsedProfile.objects.update_or_create(
            resume=resume,
            defaults={
                "name": extracted.name,
                "email": extracted.email,
                "phone": extracted.phone,
                "linkedin_url": extracted.linkedin_url,
                "other_urls": extracted.other_urls,
                "education": [e.model_dump() for e in extracted.education],
                "experience": [e.model_dump() for e in extracted.experience],
                "skills": extracted.skills,
                "certifications": extracted.certifications,
                "internships": [e.model_dump() for e in extracted.internships],
                "summary": extracted.profile_summary,
                "needs_review": needs_review,
                "extraction_method": "regex_fallback" if needs_review else "llm",
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