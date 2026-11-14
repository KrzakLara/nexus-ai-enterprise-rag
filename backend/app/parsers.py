"""Demo parsers (text PDF, DOCX, TXT); Docling/OCR integration is a planned phase."""
from io import BytesIO
import re
from docx import Document as WordDocument
from pypdf import PdfReader


def extract_pages(filename: str, data: bytes) -> list[str]:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        reader = PdfReader(BytesIO(data))
        if len(reader.pages) > 100:
            raise ValueError("Demo limit: max 100 pages")
        pages = [page.extract_text() or "" for page in reader.pages]
    elif lower.endswith(".docx"):
        doc = WordDocument(BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                paragraphs.append(" | ".join(cell.text for cell in row.cells))
        pages = ["\n".join(paragraphs)]
    elif lower.endswith((".txt", ".md")):
        pages = [data.decode("utf-8-sig")]
    else:
        raise ValueError("Supported in Phase 1: PDF (text layer), DOCX, TXT, MD")
    if not any(p.strip() for p in pages):
        raise ValueError("No selectable text found; scanned-PDF OCR is a planned feature")
    return pages


def split_page(text: str, max_chars: int = 1000, overlap: int = 150) -> list[str]:
    text = re.sub(r"[\t ]+", " ", text).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start + 500, end), text.rfind(". ", start + 500, end))
            if boundary > start + 500:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def extract_contract_fields(pages: list[str]) -> dict:
    """Transparent regex baseline with evidence; not AI/LLM extraction."""
    patterns = {
        "counterparty": r"(?im)^Counterparty:\s*(.+)$",
        "expiration_date": r"(?im)^End date:\s*(\d{4}-\d{2}-\d{2})\s*$",
        "annual_value": r"(?im)^Annual value:\s*(.+)$",
        "notice_period": r"(?im)^Notice period:\s*(.+)$",
    }
    output = {}
    for key, pattern in patterns.items():
        for number, page in enumerate(pages, 1):
            match = re.search(pattern, page)
            if match:
                output[key] = {"value": match.group(1).strip(), "page": number,
                               "evidence": match.group(0).strip(), "method": "regex", "status": "needs_review"}
                break
    return output
