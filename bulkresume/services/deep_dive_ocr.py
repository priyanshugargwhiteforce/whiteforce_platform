"""
DEEP-DIVE OCR EXTRACTOR
--------------------------
Only called from pipeline.py when compute_parse_score(...) comes back
<= OCR_DEEP_DIVE_SCORE_THRESHOLD. NOT part of the normal/default path —
existing extractors.py behavior (extract_pdf_native, extract_pdf_scanned,
extract_image, extract_docx, extract_doc_legacy) is completely untouched.

Preprocessing here now runs BOTH normal and inverted adaptive thresholding
and OCRs both, because sidebar-style resume templates often have a dark
panel with light text for the name/contact block while the rest of the
page is normal dark-on-light. A single global polarity assumption wipes
out whichever region doesn't match it.
"""

import logging
import os
import subprocess
import tempfile

import cv2
import fitz
import numpy as np
import pytesseract
from django.conf import settings
from pdf2image import convert_from_path
from PIL import Image

logger = logging.getLogger('bulkresume')

if getattr(settings, 'TESSERACT_CMD', None):
    pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD


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


def _ocr_pil_image(pil_img: Image.Image) -> str:
    """
    Runs OCR twice: once assuming dark text on light background (normal),
    once assuming light text on dark background (inverted). Sidebar/panel
    resume templates commonly have a dark contact block, so a single
    polarity pass silently drops that region. Concatenating both is safe —
    the wrong-polarity pass on any given region just yields empty/garbage
    output, which downstream LLM/regex extraction already ignores.
    """
    cv_img = cv2.cvtColor(np.array(pil_img.convert('RGB')), cv2.COLOR_RGB2BGR)
    deskewed = _deskew(cv_img)

    gray = cv2.cvtColor(deskewed, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if max(h, w) < 1800:
        scale = 1800 / max(h, w)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    gray = cv2.fastNlMeansDenoising(gray, h=10)

    normal = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
    )
    inverted = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )

    text_normal = pytesseract.image_to_string(normal, config="--oem 3 --psm 3")
    text_inverted = pytesseract.image_to_string(inverted, config="--oem 3 --psm 3")

    return (text_normal + "\n" + text_inverted).strip()


# ---------------- per-type deep-dive entry points ----------------

def _deep_dive_pdf(file_path: str) -> str:
    kwargs = {'dpi': 300}
    if getattr(settings, 'POPPLER_PATH', None):
        kwargs['poppler_path'] = settings.POPPLER_PATH
    images = convert_from_path(file_path, **kwargs)
    return "\n".join(_ocr_pil_image(img) for img in images).strip()


def _deep_dive_image(file_path: str) -> str:
    return _ocr_pil_image(Image.open(file_path)).strip()


def _deep_dive_office_doc(file_path: str) -> str:
    """docx/doc — convert to PDF via LibreOffice (same tool already used by
    extract_doc_legacy for .doc), then run the same enhanced PDF OCR path.
    Covers the case where the text LAYER is corrupted/garbled but the
    visual content is fine (e.g. bad font subsetting)."""
    soffice_cmd = getattr(settings, 'SOFFICE_PATH', 'soffice')
    with tempfile.TemporaryDirectory() as tmp_dir:
        subprocess.run(
            [soffice_cmd, '--headless', '--convert-to', 'pdf', '--outdir', tmp_dir, file_path],
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