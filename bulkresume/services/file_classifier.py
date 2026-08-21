import magic
import fitz


def classify_file(file_path: str) -> str:
    mime = magic.from_file(file_path, mime=True)

    if mime == 'application/pdf':
        return 'pdf_scanned' if _pdf_is_scanned(file_path) else 'pdf_native'
    elif mime in ('image/jpeg', 'image/png', 'image/jpg'):
        return 'image'
    elif mime == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        return 'docx'
    elif mime == 'application/msword':
        return 'doc'
    else:
        raise ValueError(f"Unsupported file type: {mime}")


def _pdf_is_scanned(file_path: str, min_chars_per_page: int = 30) -> bool:
    doc = fitz.open(file_path)
    total_chars = sum(len(page.get_text().strip()) for page in doc)
    avg_chars = total_chars / max(len(doc), 1)
    doc.close()
    return avg_chars < min_chars_per_page