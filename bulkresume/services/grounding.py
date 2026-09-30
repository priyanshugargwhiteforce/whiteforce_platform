"""
GROUNDING CHECK FOR EXTRACTED EDUCATION / EXPERIENCE
------------------------------------------------------
The prompt tells the model to copy education and job details exactly as
written, but a model still occasionally "completes" a row: an institute name
that is not on the resume, a year borrowed from another section, a company
guessed from the job title. Prompts can't guarantee that, so this checks the
result against the resume text itself and blanks anything that isn't there.

Rules (deliberately conservative -- only clearly unsupported values are
removed, wording differences are tolerated):
  - A text value is "supported" if at least 60% of its meaningful words occur
    somewhere in the resume text.
  - Every 4-digit year inside a value must occur in the resume text.
  - education: unsupported institution / year -> "". An entry whose degree AND
    institution are both unsupported is dropped as fabricated.
  - experience / internships: unsupported title / company / duration -> "". An
    entry with nothing supported left is dropped.

Pure functions over the ResumeExtraction model; no Django / LLM dependencies.
"""
import logging
import re

logger = logging.getLogger('bulkresume')

_WORD_RE = re.compile(r"[a-z0-9]+")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_STOP_WORDS = {
    "of", "the", "and", "in", "at", "for", "to", "a", "an", "on", "with", "from",
    "ltd", "pvt", "limited", "private", "inc", "llp", "co", "corp",
}
_SUPPORT_RATIO = 0.6


def _words(text) -> list:
    return [w for w in _WORD_RE.findall(str(text or "").lower()) if len(w) >= 2 and w not in _STOP_WORDS]


def _supported(value: str, corpus_words: set, corpus_lower: str) -> bool:
    """True if the value is empty, or is backed by the resume text."""
    if not value or not str(value).strip():
        return True
    for year in _YEAR_RE.findall(str(value)):
        if year not in corpus_lower:
            return False
    words = _words(value)
    if not words:
        return True   # nothing checkable (punctuation, single letters)
    return sum(1 for w in words if w in corpus_words) / len(words) >= _SUPPORT_RATIO


def _phrase_in_text(value: str, corpus_compact: str) -> bool:
    """Does the degree name (without any bracketed extras) occur as one
    contiguous run in the resume, ignoring spaces/punctuation/case? Stricter
    than the word check: "M.Tech" is not "supported" just because the word
    "tech" appears in some company name."""
    base = re.sub(r"\(.*?\)", "", str(value or ""))
    compact = re.sub(r"[^a-z0-9]+", "", base.lower())
    return not compact or compact in corpus_compact


def _ground_jobs(jobs: list, corpus_words: set, corpus_lower: str, label: str) -> list:
    kept = []
    for job in jobs:
        for attr in ("title", "company", "duration"):
            value = getattr(job, attr)
            if value and not _supported(value, corpus_words, corpus_lower):
                logger.info(f"grounding: cleared {label}.{attr}={value!r} (not found in resume text)")
                setattr(job, attr, "")
        if job.title or job.company or job.duration:
            kept.append(job)
        else:
            logger.info(f"grounding: dropped empty {label} entry")
    return kept


def ground_extraction(extracted, resume_text: str):
    """Mutates and returns `extracted` (a ResumeExtraction)."""
    corpus_lower = str(resume_text or "").lower()
    if len(corpus_lower.strip()) < 20:
        return extracted   # no reliable text to check against
    corpus_words = set(_words(corpus_lower))
    corpus_compact = re.sub(r"[^a-z0-9]+", "", corpus_lower)

    kept_education = []
    for edu in extracted.education:
        degree_ok = _supported(edu.degree, corpus_words, corpus_lower)
        institution_ok = _supported(edu.institution, corpus_words, corpus_lower)
        # A named institute that isn't in the resume, next to a degree name
        # that doesn't appear as written either, is a made-up row.
        invented_pair = bool(edu.institution) and not institution_ok and not _phrase_in_text(edu.degree, corpus_compact)
        if (not degree_ok and not institution_ok) or invented_pair:
            logger.info(f"grounding: dropped education entry {edu.degree!r} / {edu.institution!r} (not in resume text)")
            continue
        if not institution_ok:
            logger.info(f"grounding: cleared education.institution={edu.institution!r} (not found in resume text)")
            edu.institution = ""
        # Marks are mostly digits, which the word check skips when they are a
        # single character ("8.4"), so every number in the value must also
        # appear verbatim in the resume.
        if edu.percentage and not (
            _supported(edu.percentage, corpus_words, corpus_lower)
            and all(n in corpus_lower for n in re.findall(r"\d+(?:\.\d+)?", edu.percentage))
        ):
            logger.info(f"grounding: cleared education.percentage={edu.percentage!r} (not found in resume text)")
            edu.percentage = None
        if edu.year and not _supported(edu.year, corpus_words, corpus_lower):
            logger.info(f"grounding: cleared education.year={edu.year!r} (not found in resume text)")
            edu.year = ""
        kept_education.append(edu)
    extracted.education = kept_education

    kept_projects = []
    for project in getattr(extracted, "projects", []):
        # A project whose name isn't in the resume is a made-up project.
        if not project.title or not _supported(project.title, corpus_words, corpus_lower):
            logger.info(f"grounding: dropped project {project.title!r} (not found in resume text)")
            continue
        for attr in ("company", "start_date", "end_date"):
            value = getattr(project, attr)
            if value and not _supported(value, corpus_words, corpus_lower):
                logger.info(f"grounding: cleared project.{attr}={value!r} (not found in resume text)")
                setattr(project, attr, "")
        project.methodologies = [m for m in project.methodologies if _supported(m, corpus_words, corpus_lower)]
        kept_projects.append(project)
    if hasattr(extracted, "projects"):
        extracted.projects = kept_projects

    extracted.experience = _ground_jobs(extracted.experience, corpus_words, corpus_lower, "experience")
    extracted.internships = _ground_jobs(extracted.internships, corpus_words, corpus_lower, "internship")
    return extracted
