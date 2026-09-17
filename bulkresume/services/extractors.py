import logging
import os
import subprocess
import tempfile

import fitz
import pytesseract
from django.conf import settings
from docx import Document
from pdf2image import convert_from_path
from PIL import Image

logger = logging.getLogger('bulkresume')

# ── Windows-specific tool paths ────────────────────────────────────────────
if getattr(settings, 'TESSERACT_CMD', None):
    pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD

# Same safety caps as services/deep_dive_ocr.py, applied to the FIRST-pass
# extraction too -- these functions run on every scanned/image/.doc resume
# BEFORE deep-dive is even considered, so an unprotected page count or a
# stuck Tesseract call here happens more often than in the deep-dive path,
# not less.
MAX_EXTRACT_PAGES = 5
TESSERACT_TIMEOUT_SECONDS = 25


def _tesseract_ocr(img) -> str:
    """pytesseract call with a hard timeout -- without this, a corrupted or
    adversarial image can hang a worker slot indefinitely."""
    try:
        return pytesseract.image_to_string(img, timeout=TESSERACT_TIMEOUT_SECONDS)
    except RuntimeError as exc:
        logger.warning(f"Tesseract call timed out/failed after {TESSERACT_TIMEOUT_SECONDS}s: {exc}")
        return ""


def extract_pdf_native(file_path: str) -> str:
    doc = fitz.open(file_path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text.strip()


def extract_pdf_scanned(file_path: str) -> str:
    kwargs = {'dpi': 300}
    if getattr(settings, 'POPPLER_PATH', None):
        kwargs['poppler_path'] = settings.POPPLER_PATH

    # last_page caps how many pages even get rendered -- an oversized or
    # accidentally-multi-document PDF no longer forces a full render+OCR
    # pass over every page before the caller gets anything back.
    images = convert_from_path(file_path, last_page=MAX_EXTRACT_PAGES, **kwargs)
    if len(images) >= MAX_EXTRACT_PAGES:
        logger.info(f"{file_path}: capped scanned-PDF extraction at {MAX_EXTRACT_PAGES} pages")

    text_parts = []
    for img in images:
        img = img.convert('L')
        text_parts.append(_tesseract_ocr(img))
    return "\n".join(text_parts).strip()


def extract_image(file_path: str) -> str:
    img = Image.open(file_path).convert('L')
    return _tesseract_ocr(img).strip()


def extract_docx(file_path: str) -> str:
    doc = Document(file_path)
    return "\n".join(p.text for p in doc.paragraphs).strip()


def extract_doc_legacy(file_path: str) -> str:
    """
    Each call gets its OWN LibreOffice user profile (-env:UserInstallation)
    instead of the shared default one. Without this, two or more
    `soffice --headless` processes running concurrently (any time more than
    one .doc resume is being processed at once under --concurrency=5) fight
    over the same profile lock file -- a well-documented LibreOffice
    headless issue that causes hangs/failures under concurrency. This
    matters MORE here than in the deep-dive path, since every .doc resume
    goes through this function on the first pass, not just the ones that
    score badly enough to trigger deep-dive.
    """
    soffice_cmd = getattr(settings, 'SOFFICE_PATH', 'soffice')

    with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as profile_dir:
        profile_uri = f"file://{profile_dir}"
        subprocess.run(
            [
                soffice_cmd, '--headless', '--norestore',
                f'-env:UserInstallation={profile_uri}',
                '--convert-to', 'docx', '--outdir', tmp_dir, file_path,
            ],
            check=True, timeout=60,
        )
        converted = os.path.join(
            tmp_dir, os.path.splitext(os.path.basename(file_path))[0] + '.docx'
        )
        return extract_docx(converted)


EXTRACTOR_MAP = {
    'pdf_native': extract_pdf_native,
    'pdf_scanned': extract_pdf_scanned,
    'image': extract_image,
    'docx': extract_docx,
    'doc': extract_doc_legacy,
}


def extract_text(file_path: str, file_type: str) -> str:
    extractor = EXTRACTOR_MAP.get(file_type)
    if not extractor:
        raise ValueError(f"No extractor for type: {file_type}")
    text = extractor(file_path)
    if not text or len(text) < 20:
        raise ValueError("Extraction produced negligible text")
    return text