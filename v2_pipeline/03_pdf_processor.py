"""
Stage 3: PDF Processor
======================
Download and extract text from PDFs.
Uses PyMuPDF (import pymupdf) for text-based PDFs.
Falls back to OCR (pytesseract) only when the text layer is empty/useless.

MPN-Targeted extraction:
  - Product PDFs: search all pages for MPN/alternate MPN, extract matching pages
  - Catalog PDFs: same, but also include 1 neighboring page on each side
  - SDS/Spec/Manual PDFs: extract all pages that mention MPN (likely few pages)
  - If MPN not found in any page: record URL in CSV but create ZERO chunks

Outputs per PDF: dict with extracted text and processing metadata
"""

import os
import re
import time
import tempfile
import requests

try:
    import pymupdf as fitz           # preferred: newer API
except ImportError:
    import fitz                      # fallback: older PyMuPDF

# OCR support — optional. If not installed, OCR falls back to empty.
try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# Minimum chars to consider a page "has text" (avoids treating sparse pages as scanned)
MIN_TEXT_THRESHOLD = 50

# Max pages to OCR (expensive, slow — cap it)
MAX_OCR_PAGES = 5


def _normalize(text: str) -> str:
    """Normalize text for MPN matching (remove hyphens, spaces, lowercase)."""
    return re.sub(r"[\s\-]", "", text).lower()


def _mpn_found_on_page(page_text: str, mpns: list) -> bool:
    """Check if any of the provided MPNs appear on this page text."""
    page_norm = _normalize(page_text)
    for mpn in mpns:
        if _normalize(mpn) in page_norm:
            return True
        # Also try partial: last 8+ chars of MPN (handles prefix-stripped SKUs)
        if len(mpn) > 8 and mpn[-8:].lower() in page_text.lower():
            return True
    return False


def _download_pdf(url: str, timeout: int = 20) -> bytes | None:
    """Download PDF bytes from URL. Returns None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, stream=True)
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                return resp.content
        print(f"    [PDF] HTTP {resp.status_code} for {url}")
        return None
    except Exception as e:
        print(f"    [PDF] Download error for {url}: {e}")
        return None


def _page_text_via_ocr(page) -> str:
    """Extract text from a PyMuPDF page via OCR. Requires pytesseract."""
    if not OCR_AVAILABLE:
        return ""
    try:
        pix = page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        return pytesseract.image_to_string(img)
    except Exception as e:
        print(f"      [OCR] Error: {e}")
        return ""


def process_pdf(url: str, pdf_type: str, mpns: list) -> dict:
    """
    Download and process a single PDF.

    Args:
        url:      URL of the PDF to download
        pdf_type: one of: sds | catalog | specification | manual | product
        mpns:     list of MPN/alternate MPNs to search for

    Returns:
        dict with:
          source_url, pdf_type, is_text_based, total_pages,
          mpn_found_on_pages, extracted_text, char_count,
          used_ocr, ocr_pages_processed
    """
    result = {
        "source_url": url,
        "pdf_type": pdf_type,
        "is_text_based": False,
        "total_pages": 0,
        "mpn_found_on_pages": [],
        "extracted_text": "",
        "char_count": 0,
        "used_ocr": False,
        "ocr_pages_processed": 0,
        "download_failed": False,
    }

    print(f"    [PDF] Downloading {pdf_type} PDF: {url[:80]}...")
    pdf_bytes = _download_pdf(url)
    if not pdf_bytes:
        result["download_failed"] = True
        return result

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        print(f"    [PDF] Could not open PDF: {e}")
        result["download_failed"] = True
        return result

    result["total_pages"] = len(doc)
    print(f"    [PDF] Opened: {len(doc)} pages")

    # --- Step 1: Probe page 1 for text layer ---
    probe_text = doc[0].get_text() if len(doc) > 0 else ""
    is_text_based = len(probe_text.strip()) >= MIN_TEXT_THRESHOLD
    result["is_text_based"] = is_text_based

    # --- Step 2: Scan all pages ---
    page_texts = {}  # page_index → text

    if is_text_based:
        # Text-based PDF: extract text directly
        for i in range(len(doc)):
            page_text = doc[i].get_text()
            page_texts[i] = page_text
    else:
        # Scanned PDF: OCR a limited number of pages
        result["used_ocr"] = True
        print(f"    [PDF] Scanned PDF detected — using OCR (max {MAX_OCR_PAGES} pages)")
        ocr_count = 0
        for i in range(len(doc)):
            if ocr_count >= MAX_OCR_PAGES:
                break
            page_text = _page_text_via_ocr(doc[i])
            page_texts[i] = page_text
            ocr_count += 1
        result["ocr_pages_processed"] = ocr_count

    # --- Step 3: Find MPN on pages ---
    mpn_pages = []
    for i, text in page_texts.items():
        if _mpn_found_on_page(text, mpns):
            mpn_pages.append(i)

    result["mpn_found_on_pages"] = mpn_pages

    if not mpn_pages:
        print(f"    [PDF] MPN not found in PDF — URL recorded for CSV, zero evidence chunks")
        doc.close()
        return result

    print(f"    [PDF] MPN found on pages: {[p+1 for p in mpn_pages]}")

    # --- Step 4: Extract relevant pages (+ neighbors for catalog) ---
    pages_to_extract = set(mpn_pages)

    if pdf_type == "catalog":
        # Include 1 neighboring page on each side (product table may span pages)
        expanded = set()
        for p in mpn_pages:
            expanded.add(p)
            if p > 0:
                expanded.add(p - 1)
            if p < len(doc) - 1:
                expanded.add(p + 1)
        pages_to_extract = expanded
        print(f"    [PDF] Catalog: extracting pages {sorted([p+1 for p in pages_to_extract])}")

    # Build the extracted text block
    extracted_parts = []
    for i in sorted(pages_to_extract):
        if i in page_texts:
            extracted_parts.append(f"\n--- PDF PAGE {i+1} ---\n{page_texts[i]}")

    extracted_text = "\n".join(extracted_parts)
    result["extracted_text"] = extracted_text
    result["char_count"] = len(extracted_text)

    doc.close()
    print(f"    [PDF] Extracted {len(extracted_text)} chars from {len(pages_to_extract)} relevant pages")
    return result


def process_all_pdfs(pdf_urls_by_type: dict, mpns: list) -> list:
    """
    Process all PDFs for a product.

    Args:
        pdf_urls_by_type: dict from resource classifier
          {"sds": [...], "catalog": [...], "specification": [...], ...}
        mpns: list of MPNs to search for (main MPN + alternates)

    Returns:
        list of process_pdf() result dicts
    """
    results = []

    for pdf_type, urls in pdf_urls_by_type.items():
        for url in urls:
            result = process_pdf(url, pdf_type, mpns)
            results.append(result)
            time.sleep(0.5)  # polite delay between PDF downloads

    return results


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    # Test with a known product PDF
    # Using a 3M SDS as example (replace with any accessible PDF URL)
    test_url = "https://www.whirlpool.com/content/dam/global/documents/202412/owners-manual-w11323304-revj.pdf"
    test_mpns = ["WDTS7024RZ", "WDTS7024R"]

    result = process_pdf(test_url, "manual", test_mpns)
    print(json.dumps({
        "pdf_type": result["pdf_type"],
        "is_text_based": result["is_text_based"],
        "total_pages": result["total_pages"],
        "mpn_found_on_pages": [p + 1 for p in result["mpn_found_on_pages"]],
        "char_count": result["char_count"],
        "used_ocr": result["used_ocr"],
        "download_failed": result["download_failed"],
        "text_preview": result["extracted_text"][:400] if result["extracted_text"] else "",
    }, indent=2))
