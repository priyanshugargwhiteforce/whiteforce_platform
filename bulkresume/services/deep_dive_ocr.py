"""
DEEP-DIVE OCR EXTRACTOR
--------------------------
Only called from pipeline.py when compute_parse_score(...) comes back
<= OCR_DEEP_DIVE_SCORE_THRESHOLD. NOT part of the normal/default path —
existing extractors.py behavior (extract_pdf_native, extract_pdf_scanned,
extract_image, extract_docx, extract_doc_legacy) is completely untouched.

Preprocessing here runs BOTH normal and inverted adaptive thresholding
and OCRs both ONLY when the normal pass looks weak (see _ocr_pil_image) --
sidebar-style resume templates often have a dark panel with light text for
the name/contact block while the rest of the page is normal dark-on-light,
so a single global polarity assumption can wipe out that region. But most
resumes are plain dark-on-light throughout, so paying the inverted-pass
cost unconditionally on every page was pure waste for the common case.
"""

import logging
import os
import subprocess
import tempfile
import uuid

import cv2
import numpy as np
import pytesseract
from django.conf import settings
from pdf2image import convert_from_path
from PIL import Image

logger = logging.getLogger('bulkresume')

if getattr(settings, 'TESSERACT_CMD', None):
    pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD

# OpenCV spawns its own internal thread pool (TBB/pthreads, separate from
# Tesseract's OpenMP threads) for every cv2.* call below -- cvtColor,
# resize, fastNlMeansDenoising, adaptiveThreshold, warpAffine all use it,
# and by default it's one thread per CPU core, PER CALL, PER PROCESS. Force
# single-threaded mode so total CPU usage across all Celery workers stays
# bounded by --concurrency and the systemd CPUQuota, not multiplied by
# every core on the machine.
cv2.setNumThreads(1)

# Hard safety caps -- a single unusually large/corrupted upload should
# never be able to stall a worker slot for minutes while the rest of a
# bulk batch waits behind it.
MAX_DEEP_DIVE_PAGES = 5          # only re-OCR the first 5 pages of any doc
MAX_IMAGE_DIMENSION = 2200       # px -- both upscale AND downscale target
TESSERACT_TIMEOUT_SECONDS = 25   # per OCR call (so worst case ~50s/page: normal + inverted)
MIN_TEXT_LEN_TO_SKIP_INVERTED = 40  # if normal pass already found this much text, don't bother with inverted


# ---------------- preprocessing (only used in the deep-dive path) ----------------

def _deskew(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 50:
        return image
    angle = cv2.minAreaRect(coords)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) < 0.3:
        return image
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _resize_to_target(gray: np.ndarray) -> np.ndarray:
    """
    Both directions now: upscale small/low-DPI images up to
    MAX_IMAGE_DIMENSION (helps OCR accuracy on tiny scans), AND downscale
    anything bigger back down to the same cap (a 300 DPI page is often
    ~3500px tall -- fastNlMeansDenoising's cost grows fast with image size,
    so running it uncapped on a 3500px image was needless CPU burn with no
    real accuracy benefit over ~2200px for OCR purposes).
    """
    h, w = gray.shape
    largest_dim = max(h, w)
    if largest_dim == 0:
        return gray
    scale = MAX_IMAGE_DIMENSION / largest_dim
    if abs(scale - 1.0) < 0.05:  # close enough, skip a pointless resize
        return gray
    interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interpolation)


def _tesseract_ocr(image: np.ndarray) -> str:
    """pytesseract call with a hard timeout -- without this, a corrupted or
    adversarial image can hang a worker slot indefinitely (pytesseract
    raises RuntimeError('Tesseract process timeout') on timeout instead of
    hanging forever, which deep_dive_ocr_extract's caller already treats as
    a normal failure -> falls back to '')."""
    try:
        return pytesseract.image_to_string(
            image, config="--oem 3 --psm 3", timeout=TESSERACT_TIMEOUT_SECONDS
        )
    except RuntimeError as exc:
        logger.warning(f"Tesseract call timed out/failed after {TESSERACT_TIMEOUT_SECONDS}s: {exc}")
        return ""


def _ocr_pil_image(pil_img: Image.Image) -> str:
    """
    Runs the normal (dark-text-on-light-bg) OCR pass always. Only runs the
    inverted (light-text-on-dark-bg) pass if the normal pass came back
    suspiciously short -- e.g. a sidebar-template resume where the normal
    pass caught the main content but missed a dark contact-info panel. For
    the common case (plain resume, no dark panels), this halves the OCR
    cost per page instead of always paying for both passes.
    """
    cv_img = cv2.cvtColor(np.array(pil_img.convert('RGB')), cv2.COLOR_RGB2BGR)
    deskewed = _deskew(cv_img)

    gray = cv2.cvtColor(deskewed, cv2.COLOR_BGR2GRAY)
    gray = _resize_to_target(gray)
    gray = cv2.fastNlMeansDenoising(gray, h=10)

    normal = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
    )
    text_normal = _tesseract_ocr(normal)

    if len(text_normal.strip()) >= MIN_TEXT_LEN_TO_SKIP_INVERTED:
        return text_normal.strip()

    inverted = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    text_inverted = _tesseract_ocr(inverted)

    return (text_normal + "\n" + text_inverted).strip()


# ---------------- per-type deep-dive entry points ----------------

def _deep_dive_pdf(file_path: str) -> str:
    kwargs = {'dpi': 300}
    if getattr(settings, 'POPPLER_PATH', None):
        kwargs['poppler_path'] = settings.POPPLER_PATH
    # last_page caps how many pages pdf2image even renders -- a 20-page
    # PDF (accidental upload, or a corrupted/malicious file) no longer
    # forces this worker to render+OCR all 20 pages before giving up.
    images = convert_from_path(file_path, last_page=MAX_DEEP_DIVE_PAGES, **kwargs)
    if len(images) >= MAX_DEEP_DIVE_PAGES:
        logger.info(f"{file_path}: capped deep-dive OCR at {MAX_DEEP_DIVE_PAGES} pages")
    return "\n".join(_ocr_pil_image(img) for img in images).strip()


def _deep_dive_image(file_path: str) -> str:
    return _ocr_pil_image(Image.open(file_path)).strip()


def _deep_dive_office_doc(file_path: str) -> str:
    """docx/doc — convert to PDF via LibreOffice (same tool already used by
    extract_doc_legacy for .doc), then run the same enhanced PDF OCR path.
    Covers the case where the text LAYER is corrupted/garbled but the
    visual content is fine (e.g. bad font subsetting).

    Each call now gets its OWN LibreOffice user profile
    (-env:UserInstallation) instead of the shared default one. Without
    this, two or more `soffice --headless` processes running concurrently
    (exactly what happens under --concurrency=5 when several workers hit
    .doc/.docx deep-dive at the same time) fight over the same profile
    lock file -- a well-documented LibreOffice headless issue that causes
    hangs/failures under concurrency, and is a strong candidate for the
    'bulk upload specifically breaks' symptom.
    """
    soffice_cmd = getattr(settings, 'SOFFICE_PATH', 'soffice')
    with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as profile_dir:
        profile_uri = f"file://{profile_dir}"
        subprocess.run(
            [
                soffice_cmd, '--headless', '--norestore',
                f'-env:UserInstallation={profile_uri}',
                '--convert-to', 'pdf', '--outdir', tmp_dir, file_path,
            ],
            check=True, timeout=60,
        )
        converted_pdf = os.path.join(
            tmp_dir, os.path.splitext(os.path.basename(file_path))[0] + '.pdf'
        )
        return _deep_dive_pdf(converted_pdf)


DEEP_DIVE_MAP = {
    'pdf_native': _deep_dive_pdf,
    'pdf_scanned': _deep_dive_pdf,   # re-OCR with the stronger preprocessing pass
    'image': _deep_dive_image,
    'docx': _deep_dive_office_doc,
    'doc': _deep_dive_office_doc,
}


def deep_dive_ocr_extract(file_path: str, file_type: str) -> str:
    """
    Returns re-OCR'd text, or "" if this file_type isn't handled or the
    OCR pass itself fails. Callers must treat "" as "deep-dive did not
    help" and keep the original extraction — never let a deep-dive
    failure crash the main pipeline.
    """
    handler = DEEP_DIVE_MAP.get(file_type)
    if not handler:
        logger.warning(f"No deep-dive OCR handler for file_type={file_type!r}")
        return ""
    try:
        text = handler(file_path)
        return text if text and len(text) >= 20 else ""
    except Exception as exc:
        logger.warning(f"Deep-dive OCR failed for {file_path} ({file_type}): {exc}")
        return ""