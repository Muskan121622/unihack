"""
Stage 4: Evidence Builder
=========================
Merges HTML evidence + PDF evidence into two clean outputs:

A. structured_evidence — flat dict of key:value facts that go DIRECTLY to LLM
   (no embedding needed for these)
   - URL columns (mfr_url, ref_urls, resource URLs, images)
   - Parsed specs from HTML tables
   - JSON-LD fields
   - Barcodes
   - PDF-derived specs (from MPN-matched pages)

B. chunk_candidates — list of text segments that WILL be embedded
   - Description paragraphs from HTML (unstructured)
   - MPN-matched PDF pages (if not already covered by structured extraction)

Chunk selection rules (hard limits):
  - Structured HTML/JSON-LD specs → 0 chunks
  - Description paragraphs → up to 3 per page, max 10 total
  - PDF relevant pages → each becomes up to 2 chunks (split at 1200 chars with 200 overlap)
  - Maximum total per product: 20 chunks (ceiling, not target)

Also saves a per-product structured_evidence.json to artifacts/{MPN}/
"""

import re
import os
import json
from typing import Optional

MAX_CHUNKS_PER_PRODUCT = 15
PDF_CHUNK_SIZE = 1200
PDF_CHUNK_OVERLAP = 200
MIN_CHUNK_LENGTH = 80   # skip very short chunks


def _normalize_spec_key(key: str) -> str:
    """Normalize a spec table key for deduplication."""
    return re.sub(r"[\s_\-]+", " ", key.strip().lower())


def _merge_specs(existing: dict, new_specs: dict) -> None:
    """Merge new specs into existing, preferring non-empty values."""
    for k, v in new_specs.items():
        norm_k = _normalize_spec_key(k)
        if norm_k not in {_normalize_spec_key(ek) for ek in existing}:
            existing[k] = v
        elif not existing.get(k):
            existing[k] = v


def _chunk_text(text: str, size: int = PDF_CHUNK_SIZE,
                overlap: int = PDF_CHUNK_OVERLAP) -> list:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunk = text[start:end].strip()
        if len(chunk) >= MIN_CHUNK_LENGTH:
            chunks.append(chunk)
        stride = size - overlap
        if stride <= 0:
            break
        start += stride
    return chunks


def _extract_specs_from_pdf_text(pdf_text: str) -> dict:
    """
    Try to extract key:value pairs from PDF page text.
    PDFs often have lines like "Belt Width: 0.5 in" or "Grit: 150"
    """
    specs = {}
    lines = pdf_text.split("\n")
    for line in lines:
        line = line.strip()
        # Pattern: "Key: Value" or "Key — Value" or "Key  Value" (double space)
        m = re.match(r"^([A-Za-z][A-Za-z\s\-\/]{2,40})\s*[:—–]\s*(.+)$", line)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            if len(val) < 200 and val:
                specs[key] = val
    return specs


def build_evidence(
    mpn: str,
    brand: str,
    classified_resources: dict,
    html_evidences: list,
    pdf_results: list,
    alternate_mpns: list = None,
) -> tuple[dict, list, dict]:
    """
    Build structured evidence and chunk candidates for one product.

    Args:
        mpn:                  Primary MPN
        brand:                Brand name
        classified_resources: Output from Stage 1 (resource classifier)
        html_evidences:       List of parse results from Stage 2 (html_parser)
        pdf_results:          List of process results from Stage 3 (pdf_processor)
        alternate_mpns:       List of alternate MPNs (optional)

    Returns:
        (structured_evidence, chunk_candidates, processing_stats)
    """
    if alternate_mpns is None:
        alternate_mpns = []

    # ------------------------------------------------------------------ #
    # A. STRUCTURED EVIDENCE — filled deterministically, no embedding     #
    # ------------------------------------------------------------------ #
    structured = {
        # Identity
        "mpn": mpn,
        "brand": brand,
        "alternate_mpns": alternate_mpns,

        # URL columns (go directly into CSV)
        "mfr_url": classified_resources.get("mfr_url"),
        "ref_urls": classified_resources.get("ref_urls", []),
        "video_url": classified_resources["video_urls"][0] if classified_resources.get("video_urls") else None,

        # Image columns — from discovery image_urls + HTML page images
        "product_image": None,
        "alt_images": [],

        # Document URL columns
        "sds_url": None,
        "sds_1_url": None,
        "catalog_url": None,
        "specification_url": None,
        "manual_url": None,
        "service_manual_url": None,
        "owners_manual_url": None,

        # Parsed specs (merged from all sources)
        "parsed_specs": {},

        # Barcodes
        "UPC": None,
        "EAN": None,
        "GTIN": None,

        # Identity fields from JSON-LD
        "product_name": None,
        "sku": None,
        "list_price": None,

        # From any source
        "country_of_origin": None,
    }

    # --- Populate document URL columns from classified PDF resources ---
    pdf_urls = classified_resources.get("pdf_urls", {})

    if pdf_urls.get("sds"):
        structured["sds_url"] = pdf_urls["sds"][0]
        if len(pdf_urls["sds"]) > 1:
            structured["sds_1_url"] = pdf_urls["sds"][1]

    if pdf_urls.get("catalog"):
        structured["catalog_url"] = pdf_urls["catalog"][0]

    if pdf_urls.get("specification"):
        structured["specification_url"] = pdf_urls["specification"][0]

    if pdf_urls.get("manual"):
        # Try to split manual types by URL hints
        manuals = pdf_urls["manual"]
        for m_url in manuals:
            m_lower = m_url.lower()
            if "service" in m_lower and not structured["service_manual_url"]:
                structured["service_manual_url"] = m_url
            elif "owner" in m_lower or "user" in m_lower:
                structured["owners_manual_url"] = m_url
            elif not structured["manual_url"]:
                structured["manual_url"] = m_url

    # Also pick up PDF links found during HTML parsing
    for html_ev in html_evidences:
        for pdf_link in html_ev.get("pdf_links", []):
            link_url = pdf_link["url"]
            label = pdf_link["label"].lower()
            if not structured["sds_url"] and any(k in label for k in ["sds", "safety", "msds"]):
                structured["sds_url"] = link_url
            elif not structured["catalog_url"] and any(k in label for k in ["catalog", "brochure"]):
                structured["catalog_url"] = link_url
            elif not structured["specification_url"] and any(k in label for k in ["spec", "datasheet", "tds", "technical"]):
                structured["specification_url"] = link_url
            elif not structured["manual_url"] and any(k in label for k in ["manual", "install", "instruction"]):
                structured["manual_url"] = link_url

    # --- Populate image columns ---
    # Priority: images from discovery (already in classified_resources.image_urls)
    # Then: images extracted from HTML pages (prefer MPN-specific ones)
    all_images = list(classified_resources.get("image_urls", []))

    # Add images from HTML parsing (merge, deduplicate)
    seen_images = set(all_images)
    for html_ev in html_evidences:
        for img_url in html_ev.get("images", []):
            if img_url not in seen_images:
                all_images.append(img_url)
                seen_images.add(img_url)

    if all_images:
        structured["product_image"] = all_images[0]
        structured["alt_images"] = all_images[1:5]

    # --- Merge HTML spec tables ---
    for html_ev in html_evidences:
        _merge_specs(structured["parsed_specs"], html_ev.get("specs", {}))

        # JSON-LD fields
        jl = html_ev.get("json_ld", {})
        if jl.get("product_name") and not structured["product_name"]:
            structured["product_name"] = jl["product_name"]
        if jl.get("sku") and not structured["sku"]:
            structured["sku"] = jl["sku"]
        if jl.get("gtin") and not structured["GTIN"]:
            structured["GTIN"] = jl["gtin"]
        if jl.get("list_price") and not structured["list_price"]:
            structured["list_price"] = jl["list_price"]

        # Product name fallback
        if html_ev.get("product_name") and not structured["product_name"]:
            structured["product_name"] = html_ev["product_name"]

        # Barcodes
        barcodes = html_ev.get("barcodes", {})
        if barcodes.get("UPC") and not structured["UPC"]:
            structured["UPC"] = barcodes["UPC"]
        if barcodes.get("EAN") and not structured["EAN"]:
            structured["EAN"] = barcodes["EAN"]
        if barcodes.get("GTIN") and not structured["GTIN"]:
            structured["GTIN"] = barcodes["GTIN"]

    # --- Merge PDF-extracted structured specs ---
    pdf_specs_extracted = 0
    for pdf_res in pdf_results:
        if not pdf_res.get("extracted_text"):
            continue
        pdf_specs = _extract_specs_from_pdf_text(pdf_res["extracted_text"])
        if pdf_specs:
            _merge_specs(structured["parsed_specs"], pdf_specs)
            pdf_specs_extracted += len(pdf_specs)

    # ------------------------------------------------------------------ #
    # B. CHUNK CANDIDATES — only text that needs embedding               #
    # ------------------------------------------------------------------ #
    chunk_candidates = []  # list of {"text": str, "source_type": str, "source_url": str, ...}
    stats = {
        "html_pages_parsed": len(html_evidences),
        "pdf_urls_processed": len(pdf_results),
        "structured_specs_extracted": len(structured["parsed_specs"]) + pdf_specs_extracted,
        "chunks_candidate": 0,
        "chunks_created": 0,
        "chunks_skipped_structured": 0,
        "chunks_skipped_irrelevant": 0,
        "pdf_pages_total": sum(r.get("total_pages", 0) for r in pdf_results),
        "pdf_pages_relevant": sum(len(r.get("mpn_found_on_pages", [])) for r in pdf_results),
        "pdfs_text_based": sum(1 for r in pdf_results if r.get("is_text_based")),
        "pdfs_ocr": sum(1 for r in pdf_results if r.get("used_ocr")),
        "pdfs_mpn_not_found": sum(1 for r in pdf_results if not r.get("mpn_found_on_pages")),
    }

    # HTML: description paragraphs only (specs already captured above)
    for html_ev in html_evidences:
        paragraphs = html_ev.get("description_paragraphs", [])
        stats["chunks_skipped_structured"] += len(html_ev.get("specs", {}))
        for para in paragraphs[:3]:  # max 3 per page
            stats["chunks_candidate"] += 1
            if len(para) >= MIN_CHUNK_LENGTH:
                chunk_candidates.append({
                    "text": para,
                    "source_type": "html_description",
                    "source_url": html_ev.get("source_url", ""),
                })
            else:
                stats["chunks_skipped_irrelevant"] += 1

    # PDF: MPN-matched pages → chunks
    for pdf_res in pdf_results:
        text = pdf_res.get("extracted_text", "")
        if not text:
            stats["chunks_skipped_irrelevant"] += 1
            continue

        # Don't re-chunk if we already got structured specs from this PDF
        pdf_chunks = _chunk_text(text, PDF_CHUNK_SIZE, PDF_CHUNK_OVERLAP)
        for chunk in pdf_chunks:
            stats["chunks_candidate"] += 1
            if len(chunk) >= MIN_CHUNK_LENGTH:
                chunk_candidates.append({
                    "text": chunk,
                    "source_type": f"pdf_{pdf_res.get('pdf_type', 'unknown')}",
                    "source_url": pdf_res.get("source_url", ""),
                    "is_ocr": pdf_res.get("used_ocr", False),
                })
            else:
                stats["chunks_skipped_irrelevant"] += 1

    # Apply ceiling: max 20 chunks per product
    if len(chunk_candidates) > MAX_CHUNKS_PER_PRODUCT:
        print(f"    [Evidence] Trimming {len(chunk_candidates)} → {MAX_CHUNKS_PER_PRODUCT} chunks (ceiling)")
        chunk_candidates = chunk_candidates[:MAX_CHUNKS_PER_PRODUCT]

    stats["chunks_created"] = len(chunk_candidates)
    stats["structured_evidence_fields"] = _count_nonempty(structured)

    print(
        f"    [Evidence] Structured fields: {stats['structured_evidence_fields']} | "
        f"Specs parsed: {len(structured['parsed_specs'])} | "
        f"Chunks for embedding: {stats['chunks_created']}"
    )

    return structured, chunk_candidates, stats


def _count_nonempty(d: dict) -> int:
    """Count non-null, non-empty-string, non-empty-list values in a dict."""
    count = 0
    for v in d.values():
        if isinstance(v, dict):
            count += _count_nonempty(v)
        elif isinstance(v, list) and v:
            count += 1
        elif v and v != "":
            count += 1
    return count


def save_evidence(mpn: str, artifacts_base: str,
                  structured: dict, chunk_candidates: list, stats: dict) -> None:
    """Save structured evidence and chunk candidates to artifact files."""
    artifact_dir = os.path.join(artifacts_base, mpn)
    os.makedirs(artifact_dir, exist_ok=True)

    with open(os.path.join(artifact_dir, "v2_structured_evidence.json"), "w", encoding="utf-8") as f:
        json.dump(structured, f, indent=2, ensure_ascii=False)

    with open(os.path.join(artifact_dir, "v2_chunk_candidates.json"), "w", encoding="utf-8") as f:
        json.dump(chunk_candidates, f, indent=2, ensure_ascii=False)

    print(f"    [Evidence] Saved structured evidence + {len(chunk_candidates)} chunk candidates")
