"""
Stage 4 — Evidence Builder
===========================
Merges HTML + PDF evidence into two outputs:

A) structured_evidence — flat dict of all known facts (direct to LLM, no embedding)
   Covers: URL columns, images, barcodes, parsed specs, JSON-LD fields, doc URLs

B) chunk_candidates — list of text segments that need Voyage embedding
   Policy:
     - HTML spec tables  → 0 chunks  (already structured)
     - JSON-LD fields    → 0 chunks  (already structured)
     - HTML descriptions → max 3 per page, 10 total
     - PDF MPN pages     → chunked at 1200 chars / 200 overlap
     - HARD CEILING      → 20 chunks per product (not a target, an absolute cap)

Also records granular stats for evidence_debug_5_rows.json.
"""

from __future__ import annotations
import re
import os
import json

MAX_CHUNKS = 15
PDF_CHUNK_SIZE    = 1200
PDF_CHUNK_OVERLAP = 200
MIN_CHUNK_LEN     = 80
MAX_HTML_DESC_PER_PAGE = 3
MAX_HTML_DESC_TOTAL    = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_key(k: str) -> str:
    return re.sub(r"[\s_\-]+", " ", k.strip().lower())


def _merge(dest: dict, src: dict) -> None:
    """Merge src into dest, preferring non-empty values. No overwrite."""
    norm_dest = {_norm_key(k) for k in dest}
    for k, v in src.items():
        if v and _norm_key(k) not in norm_dest:
            dest[k] = v
            norm_dest.add(_norm_key(k))


def _chunk(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        end   = min(start + PDF_CHUNK_SIZE, len(text))
        piece = text[start:end].strip()
        if len(piece) >= MIN_CHUNK_LEN:
            chunks.append(piece)
        stride = PDF_CHUNK_SIZE - PDF_CHUNK_OVERLAP
        if stride <= 0:
            break
        start += stride
    return chunks


def _pdf_kv(text: str) -> dict:
    """Extract key: value pairs from PDF page text (common in spec sheets)."""
    specs = {}
    for line in text.split("\n"):
        m = re.match(r"^([A-Za-z][A-Za-z /\-]{2,50})\s*[:—–]\s*(.+)$", line.strip())
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            if len(v) < 300:
                specs.setdefault(k, v)
    return specs


def _count_nonempty(d: dict) -> int:
    count = 0
    for v in d.values():
        if isinstance(v, dict):
            count += _count_nonempty(v)
        elif isinstance(v, list) and v:
            count += 1
        elif v not in (None, "", [], {}):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_evidence(
    mpn: str,
    brand: str,
    classified: dict,
    html_evidences: list,
    pdf_results: list,
    alternate_mpns: list | None = None,
) -> tuple[dict, list, dict]:
    """
    Returns (structured_evidence, chunk_candidates, stats)
    """
    alternate_mpns = alternate_mpns or []

    # ------------------------------------------------------------------ #
    # A.  STRUCTURED EVIDENCE                                             #
    # ------------------------------------------------------------------ #
    ev: dict = {
        # Identity
        "mpn":            mpn,
        "brand":          brand,
        "alternate_mpns": alternate_mpns,

        # CSV URL columns
        "mfr_url":        classified.get("mfr_url"),
        "ref_urls":       classified.get("ref_urls", []),

        # Images (Product Image + Alternate Image 1-4)
        "product_image":  None,
        "alt_images":     [],

        # Document URL columns (252-schema exact names)
        "SDS":            None,
        "SDS_1":          None,
        "Catalog":        None,
        "Specification Sheet": None,
        "Instruction/Installation Manual": None,
        "Service Manual": None,
        "Owners/User Manual": None,
        "Warranty Information": None,
        "Video Link":     None,
        "Video Link 1":   None,
        "Line Drawing":   None,
        "MTR":            None,
        "RoHS":           None,
        "Full Engineering Drawing": None,
        "Energy Star Guide":        None,
        "Technical Bulletin":       None,
        "Submittal":      None,
        "Compatibility Chart":      None,
        "Size Chart":     None,
        "Product Label/Insert":     None,

        # Parsed specs (key→value, merged from all HTML pages & PDFs)
        "parsed_specs":   {},

        # Barcodes
        "UPC":  None,
        "EAN":  None,
        "GTIN": None,

        # Identity from JSON-LD / HTML
        "product_name":     None,
        "sku":              None,
        "list_price":       None,
        "country_of_origin":None,
        "warranty":         None,
        "manufacturer_name":None,

        # Features (list)
        "item_features": [],
    }

    # ---- PDF classified URL columns ----
    pdf_urls = classified.get("pdf_urls", {})

    sds_list  = pdf_urls.get("sds", [])
    if sds_list:
        ev["SDS"]   = sds_list[0]
        if len(sds_list) > 1:
            ev["SDS_1"] = sds_list[1]

    if pdf_urls.get("catalog"):
        ev["Catalog"] = pdf_urls["catalog"][0]

    if pdf_urls.get("specification"):
        ev["Specification Sheet"] = pdf_urls["specification"][0]

    for m_url in pdf_urls.get("manual", []):
        ml = m_url.lower()
        if "service" in ml and not ev["Service Manual"]:
            ev["Service Manual"] = m_url
        elif ("owner" in ml or "user" in ml) and not ev["Owners/User Manual"]:
            ev["Owners/User Manual"] = m_url
        elif not ev["Instruction/Installation Manual"]:
            ev["Instruction/Installation Manual"] = m_url

    # Video
    vids = classified.get("video_urls", [])
    if vids:
        ev["Video Link"]   = vids[0]
        if len(vids) > 1:
            ev["Video Link 1"] = vids[1]

    # ---- Images: discovery first, then HTML pages ----
    all_images: list[str] = list(classified.get("image_urls", []))
    seen_images: set[str] = set(all_images)
    for html_ev in html_evidences:
        for img in html_ev.get("images", []):
            if img not in seen_images:
                all_images.append(img)
                seen_images.add(img)
    if all_images:
        ev["product_image"] = all_images[0]
        ev["alt_images"]    = all_images[1:5]

    # ---- Merge HTML structured evidence ----
    for html_ev in html_evidences:
        _merge(ev["parsed_specs"], html_ev.get("specs", {}))

        # JSON-LD
        jl = html_ev.get("json_ld", {})
        for field, ld_key in [
            ("product_name",     "product_name"),
            ("sku",              "sku"),
            ("GTIN",             "gtin"),
            ("list_price",       "list_price"),
            ("warranty",         "warranty"),
            ("country_of_origin","country_of_origin"),
            ("manufacturer_name","manufacturer"),
            ("brand",            "brand"),
        ]:
            if jl.get(ld_key) and not ev.get(field):
                ev[field] = jl[ld_key]

        # product_name fallback
        if html_ev.get("product_name") and not ev["product_name"]:
            ev["product_name"] = html_ev["product_name"]

        # Barcodes
        for bc in ["UPC", "EAN", "GTIN"]:
            if html_ev.get("barcodes", {}).get(bc) and not ev[bc]:
                ev[bc] = html_ev["barcodes"][bc]

        # Features (first page that has them wins)
        if not ev["item_features"] and html_ev.get("features"):
            ev["item_features"] = html_ev["features"][:20]

        # PDF links from HTML pages → fill doc URL columns
        for link in html_ev.get("pdf_links", []):
            url, label = link["url"], link["label"].lower()
            if not ev["SDS"] and any(k in label for k in ["sds", "safety", "msds"]):
                ev["SDS"] = url
            elif not ev["Catalog"] and any(k in label for k in ["catalog", "brochure"]):
                ev["Catalog"] = url
            elif not ev["Specification Sheet"] and any(k in label for k in ["spec", "datasheet", "tds", "technical"]):
                ev["Specification Sheet"] = url
            elif not ev["Instruction/Installation Manual"] and any(k in label for k in ["manual", "install", "instruction"]):
                ev["Instruction/Installation Manual"] = url
            elif not ev["Warranty Information"] and "warrant" in label:
                ev["Warranty Information"] = url
            elif not ev["RoHS"] and "rohs" in label:
                ev["RoHS"] = url
            elif not ev["Energy Star Guide"] and "energy star" in label:
                ev["Energy Star Guide"] = url

    # ---- Merge PDF-extracted structured specs ----
    pdf_spec_count = 0
    for pdf_res in pdf_results:
        if not pdf_res.get("extracted_text"):
            continue
        kv = _pdf_kv(pdf_res["extracted_text"])
        _merge(ev["parsed_specs"], kv)
        pdf_spec_count += len(kv)

    # ------------------------------------------------------------------ #
    # B.  CHUNK CANDIDATES                                                #
    # ------------------------------------------------------------------ #
    chunks: list[dict] = []
    stats_skipped_structured = 0
    stats_skipped_irrelevant = 0
    candidate_count = 0

    # HTML descriptions (unstructured text only — specs already captured above)
    html_desc_total = 0
    for html_ev in html_evidences:
        stats_skipped_structured += len(html_ev.get("specs", {}))
        added = 0
        for para in html_ev.get("description_paragraphs", []):
            candidate_count += 1
            if len(para) >= MIN_CHUNK_LEN and added < MAX_HTML_DESC_PER_PAGE:
                chunks.append({
                    "text":        para,
                    "source_type": "html_description",
                    "source_url":  html_ev.get("source_url", ""),
                })
                added += 1
                html_desc_total += 1
            else:
                stats_skipped_irrelevant += 1
        if html_desc_total >= MAX_HTML_DESC_TOTAL:
            break

    # PDF pages → chunks
    for pdf_res in pdf_results:
        text = pdf_res.get("extracted_text", "")
        if not text:
            stats_skipped_irrelevant += 1
            continue
        for piece in _chunk(text):
            candidate_count += 1
            chunks.append({
                "text":        piece,
                "source_type": f"pdf_{pdf_res.get('pdf_type', 'unknown')}",
                "source_url":  pdf_res.get("source_url", ""),
                "is_ocr":      pdf_res.get("used_ocr", False),
            })

    # Hard ceiling
    if len(chunks) > MAX_CHUNKS:
        print(f"    [Evidence] Trimming {len(chunks)} → {MAX_CHUNKS} chunks")
        chunks = chunks[:MAX_CHUNKS]

    ev_fields = _count_nonempty(ev)

    stats = {
        "html_pages_parsed":         len(html_evidences),
        "pdf_urls_processed":        len(pdf_results),
        "pdfs_text_based":           sum(1 for r in pdf_results if r.get("is_text_based")),
        "pdfs_ocr":                  sum(1 for r in pdf_results if r.get("used_ocr")),
        "pdfs_mpn_not_found":        sum(1 for r in pdf_results if not r.get("mpn_found_on_pages")),
        "pdf_pages_total":           sum(r.get("total_pages", 0) for r in pdf_results),
        "pdf_pages_relevant":        sum(len(r.get("mpn_found_on_pages", [])) for r in pdf_results),
        "structured_specs_extracted":len(ev["parsed_specs"]) + pdf_spec_count,
        "structured_evidence_fields":ev_fields,
        "chunks_candidate":          candidate_count,
        "chunks_created":            len(chunks),
        "chunks_skipped_structured": stats_skipped_structured,
        "chunks_skipped_irrelevant": stats_skipped_irrelevant,
    }

    print(
        f"    [Evidence] Structured fields={ev_fields} | "
        f"Specs={len(ev['parsed_specs'])} | Chunks={len(chunks)}"
    )
    return ev, chunks, stats


def save_evidence(mpn: str, artifacts_base: str,
                  ev: dict, chunks: list, stats: dict) -> None:
    safe_mpn = __import__('re').sub(r'[\\/*?:"<>|]', '_', mpn)
    d = os.path.join(artifacts_base, safe_mpn)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "v2_structured_evidence.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, indent=2, ensure_ascii=False)
    with open(os.path.join(d, "v2_chunk_candidates.json"), "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)
    print(f"    [Evidence] Saved — {len(chunks)} chunks")
