"""
Stage 3 — PDF Processor
========================
Downloads each PDF and extracts only the pages that contain the MPN.
No embedding of entire PDFs. No blind chunking.

Rules:
  - Text-based PDF → PyMuPDF direct text extraction
  - Scanned PDF (text layer < 50 chars on page 0) → pytesseract OCR, max 5 pages
  - Catalog PDFs → MPN page + 1 neighbour on each side (table may span pages)
  - MPN not found → URL still recorded for CSV, zero evidence chunks

Uses `import pymupdf as fitz` (newer API); falls back to `import fitz`.
"""

from __future__ import annotations
import re
import time
import requests

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

try:
    from PIL import Image
    import pytesseract
    # Configure Tesseract path for Windows
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    _OCR_OK = True
except ImportError:
    _OCR_OK = False

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
_MIN_TEXT = 50       # chars to consider a page text-based
_MAX_OCR_PAGES = 5   # cap OCR pages (expensive)
_DOWNLOAD_TIMEOUT = 25


def _normalise(text: str) -> str:
    return re.sub(r"[\s\-]", "", text).lower()


def _mpn_on_page(page_text: str, mpns: list) -> bool:
    norm = _normalise(page_text)
    for mpn in mpns:
        if _normalise(mpn) in norm:
            return True
        # Also match last 8+ chars (handles vendor-prefixed SKUs)
        if len(mpn) > 8 and mpn[-8:].lower() in page_text.lower():
            return True
    return False


def _download(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_DOWNLOAD_TIMEOUT, stream=True)
        if r.status_code == 200:
            ct = r.headers.get("Content-Type", "")
            if "pdf" in ct or ".pdf" in url.lower():
                return r.content
        print(f"    [PDF] HTTP {r.status_code}: {url[:70]}")
    except Exception as e:
        print(f"    [PDF] Download error: {e}")
    return None


def _ocr_page(page) -> str:
    if not _OCR_OK:
        return ""
    try:
        pix = page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        return pytesseract.image_to_string(img)
    except Exception as e:
        print(f"    [PDF] OCR error: {e}")
        return ""


def process_pdf(url: str, pdf_type: str, mpns: list) -> dict:
    """
    Download and process one PDF.

    Returns dict:
      source_url, pdf_type, is_text_based, total_pages,
      mpn_found_on_pages, extracted_text, char_count,
      used_ocr, ocr_pages, download_failed
    """
    result = {
        "source_url": url, "pdf_type": pdf_type,
        "is_text_based": False, "total_pages": 0,
        "mpn_found_on_pages": [], "extracted_text": "",
        "char_count": 0, "used_ocr": False,
        "ocr_pages": 0, "download_failed": False,
    }
    print(f"    [PDF] Downloading ({pdf_type}): {url[:75]}")
    raw = _download(url)
    if not raw:
        result["download_failed"] = True
        return result

    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as e:
        print(f"    [PDF] Open failed: {e}")
        result["download_failed"] = True
        return result

    n = len(doc)
    result["total_pages"] = n
    print(f"    [PDF] Pages: {n}")

    # Probe page 0 to decide text vs scanned
    probe = doc[0].get_text() if n > 0 else ""
    is_text = len(probe.strip()) >= _MIN_TEXT
    result["is_text_based"] = is_text

    page_texts: dict[int, str] = {}
    if is_text:
        for i in range(n):
            page_texts[i] = doc[i].get_text()
    else:
        result["used_ocr"] = True
        print(f"    [PDF] Scanned -> OCR (max {_MAX_OCR_PAGES} pages)")
        for i in range(min(n, _MAX_OCR_PAGES)):
            page_texts[i] = _ocr_page(doc[i])
        result["ocr_pages"] = len(page_texts)

    # Find MPN pages
    mpn_pages = [i for i, t in page_texts.items() if _mpn_on_page(t, mpns)]
    result["mpn_found_on_pages"] = mpn_pages

    if not mpn_pages:
        print(f"    [PDF] MPN not found — URL recorded, zero chunks")
        doc.close()
        return result

    print(f"    [PDF] MPN on pages: {[p+1 for p in mpn_pages]}")

    # Expand for catalog: ±1 page
    to_extract: set[int] = set(mpn_pages)
    if pdf_type == "catalog":
        for p in list(mpn_pages):
            if p > 0:     to_extract.add(p - 1)
            if p < n - 1: to_extract.add(p + 1)

    parts = []
    for i in sorted(to_extract):
        if i in page_texts:
            parts.append(f"\n--- PDF PAGE {i+1} ---\n{page_texts[i]}")

    result["extracted_text"] = "\n".join(parts)
    result["char_count"] = len(result["extracted_text"])
    doc.close()
    print(f"    [PDF] Extracted {result['char_count']} chars from {len(to_extract)} pages")
    return result


def process_all_pdfs(pdf_urls_by_type: dict, mpns: list) -> list:
    results = []
    for pdf_type, urls in pdf_urls_by_type.items():
        for url in urls:
            results.append(process_pdf(url, pdf_type, mpns))
            time.sleep(0.5)
    return results
