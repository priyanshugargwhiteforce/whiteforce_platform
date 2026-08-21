import os
import subprocess
import tempfile

import fitz
import pytesseract
from django.conf import settings
from docx import Document
from pdf2image import convert_from_path
from PIL import Image

# ── Windows-specific tool paths ────────────────────────────────────────────
if getattr(settings, 'TESSERACT_CMD', None):
    pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD


def extract_pdf_native(file_path: str) -> str:
    doc = fitz.open(file_path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text.strip()


def extract_pdf_scanned(file_path: str) -> str:
    kwargs = {'dpi': 300}
    if getattr(settings, 'POPPLER_PATH', None):
        kwargs['poppler_path'] = settings.POPPLER_PATH

    images = convert_from_path(file_path, **kwargs)
    text_parts = []
    for img in images:
        img = img.convert('L')
        text_parts.append(pytesseract.image_to_string(img))
    return "\n".join(text_parts).strip()


def extract_image(file_path: str) -> str:
    img = Image.open(file_path).convert('L')
    return pytesseract.image_to_string(img).strip()


def extract_docx(file_path: str) -> str:
    doc = Document(file_path)
    return "\n".join(p.text for p in doc.paragraphs).strip()


def extract_doc_legacy(file_path: str) -> str:
    soffice_cmd = getattr(settings, 'SOFFICE_PATH', 'soffice')

    with tempfile.TemporaryDirectory() as tmp_dir:
        subprocess.run(
            [soffice_cmd, '--headless', '--convert-to', 'docx', '--outdir', tmp_dir, file_path],
            check=True, timeout=60
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