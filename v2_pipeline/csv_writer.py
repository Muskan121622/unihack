"""
Stage 8 — CSV Writer + Evidence Debug
=======================================
Produces:
  1. final_delivery_5_rows.csv  — exact 252-column schema
  2. evidence_debug_5_rows.json — full per-product pipeline stats

Column priority order when writing a CSV cell:
  1. Input CSV passthrough (MPN, Dept, Class, brand columns…)
  2. Structured evidence (URL columns, images, doc URLs, barcodes)
  3. LLM output (descriptions, features, attributes, dimensions…)

Document URL validation — a URL is only written to a doc column if it:
  - Is a real PDF (contains .pdf, /pdf/, or download=true)
  - Does NOT match catalogue/search/anchor discard patterns

Image URL validation — must have a photo extension (no SVG).
Video URL validation — must be an individual YouTube/Vimeo watch URL.
"""

from __future__ import annotations
import re
import csv
import json
import os
from urllib.parse import urlparse

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_VIDEO_RE   = re.compile(
    r"youtube\.com/watch\?|youtu\.be/[\w\-]|vimeo\.com/\d+", re.I
)
_DOC_DISCARD_RE = re.compile(
    r"/catalogsearch/|/search\?|/category/|sales-specials|#[a-z]|"
    r"\?filters=|/clearance/|/deals/", re.I
)

# Exact CSV column names for all 252 schema fields
# (used to build a complete ordered mapping)
_FIXED_FIELD_MAP = {
    # llm_key: csv_column_name
    "MANUFACTURER_NAME":              "MANUFACTURER_NAME",
    "BRAND_NAME":                     "BRAND_NAME",
    "TRADE_NAME":                     "TRADE_NAME",
    "MANUFACTURER_PART_NUMBER":       "MANUFACTURER_PART_NUMBER",
    "ALTERNATE_PART_NUMBER":          "ALTERNATE_PART_NUMBER",
    "Classpath":                      "Classpath",
    "MOBILE_DESC":                    "MOBILE_DESC",
    "INVOICE_DESC":                   "INVOICE_DESC",
    "SHORT_DESC":                     "SHORT_DESC",
    "LONG_DESC1":                     "LONG_DESC1",
    "RETAIL_DESC":                    "RETAIL_DESC",
    "MARKETING_DESCRIPTION":          "MARKETING_DESCRIPTION",
    "With":                           "With",
    "Standard_Approvals":             "Standard/Approvals",
    "Prop_65":                        "Prop 65",
    "Application":                    "Application",
    "Includes":                       "Includes",
    "Product_Name":                   "Product Name",
    "UPC":                            "UPC",
    "EAN":                            "EAN",
    "GTIN":                           "GTIN",
    "UNSPSC":                         "UNSPSC",
    "Warranty":                       "Warranty",
    "List_Price":                     "List Price",
    "Selling_Qty":                    "Selling Qty",
    "Selling_UOM":                    "Selling UOM",
    "Standard_Packaging_Information": "Standard Packaging Information",
    "LENGTH":      "LENGTH",   "LENGTH_UOM": "LENGTH_UOM",
    "HEIGHT":      "HEIGHT",   "HEIGHT_UOM": "HEIGHT_UOM",
    "WIDTH":       "WIDTH",    "WIDTH_UOM":  "WIDTH_UOM",
    "WEIGHT":      "WEIGHT",   "WEIGHT_UOM": "WEIGHT_UOM",
    "VOLUME":      "VOLUME",   "VOLUME_UOM": "VOLUME_UOM",
    "Country_Of_Origin": "Country Of Origin",
    "Discontinued":      "Discontinued",
}

# Structured evidence key → CSV column name for URL/image/doc columns
_EV_DOC_COLS = [
    ("SDS",                           "SDS"),
    ("SDS_1",                         "SDS_1"),
    ("Warranty Information",          "Warranty Information"),
    ("Catalog",                       "Catalog"),
    ("Specification Sheet",           "Specification Sheet"),
    ("Instruction/Installation Manual","Instruction/Installation Manual"),
    ("Service Manual",                "Service Manual"),
    ("Owners/User Manual",            "Owners/User Manual"),
    ("Line Drawing",                  "Line Drawing"),
    ("MTR",                           "MTR"),
    ("RoHS",                          "RoHS"),
    ("Full Engineering Drawing",      "Full Engineering Drawing"),
    ("Energy Star Guide",             "Energy Star Guide"),
    ("Technical Bulletin",            "Technical Bulletin"),
    ("Submittal",                     "Submittal"),
    ("Compatibility Chart",           "Compatibility Chart"),
    ("Size Chart",                    "Size Chart"),
    ("Product Label/Insert",          "Product Label/Insert"),
    ("Video Link",                    "Video Link"),
    ("Video Link 1",                  "Video Link 1"),
]


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _valid_doc(url: str) -> bool:
    if not url: return False
    ul = url.lower()
    if _DOC_DISCARD_RE.search(ul): return False
    return (
        ".pdf" in ul or "/pdf/" in ul or "download=true" in ul
        or "/manuals/" in ul or "/documents/" in ul
    )


def _valid_image(url: str) -> bool:
    if not url: return False
    path = urlparse(url).path.lower().split("?")[0]
    return any(path.endswith(e) for e in _IMAGE_EXTS)


def _valid_video(url: str) -> bool:
    return bool(url and _VIDEO_RE.search(url))


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def build_csv_row(
    headers: list,
    input_row: dict,
    ev: dict,
    llm: dict,
) -> dict:
    row = {h: "" for h in headers}

    # ---- 1. Input passthrough ----
    for col in [
        "Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand",
        "DIB_Brand", "Part_Manuf", "PART_NUMBER", "Dept", "Class",
        "Fine", "SKU - MY_PART_NUMBER",
    ]:
        if col in row:
            row[col] = input_row.get(col, "")

    # ---- 2. URL columns from structured evidence ----
    if "MFR URL" in row:
        row["MFR URL"] = ev.get("mfr_url") or ""

    for i, url in enumerate(ev.get("ref_urls", [])[:5]):
        col = f"Ref URL {i+1}"
        if col in row:
            row[col] = url

    # Images
    pi = ev.get("product_image")
    if pi and _valid_image(pi) and "Product Image" in row:
        row["Product Image"] = pi
    for i, img in enumerate(ev.get("alt_images", [])[:4]):
        col = f"Alternate Image {i+1}"
        if col in row and _valid_image(img):
            row[col] = img

    # Document URL columns
    for ev_key, csv_col in _EV_DOC_COLS:
        if csv_col not in row:
            continue
        val = ev.get(ev_key)
        if ev_key in ("Video Link", "Video Link 1"):
            if val and _valid_video(val):
                row[csv_col] = val
        else:
            if val and _valid_doc(val):
                row[csv_col] = val

    # ---- 3. LLM fixed fields ----
    for llm_key, csv_col in _FIXED_FIELD_MAP.items():
        if csv_col not in row:
            continue
        val = llm.get(llm_key, "")
        if val and not isinstance(val, (list, dict)):
            row[csv_col] = str(val)

    # Barcode fallback: structured evidence wins over empty LLM output
    for bc, col in [("UPC", "UPC"), ("EAN", "EAN"), ("GTIN", "GTIN")]:
        if not row.get(col) and ev.get(bc):
            row[col] = ev[bc]

    # ---- 4. ITEM_FEATURES_1 … _20 ----
    features = llm.get("ITEM_FEATURES", [])
    if not features and ev.get("item_features"):
        features = ev["item_features"]
    if isinstance(features, list):
        for i, f in enumerate(features[:20]):
            col = f"ITEM_FEATURES_{i+1}"
            if col in row:
                row[col] = str(f)

    # ---- 5. ATTRIBUTE_LABEL/VALUE/UOM 1…50 ----
    attrs = llm.get("dynamic_attributes", [])
    if isinstance(attrs, list):
        # Filter out pricing rows, handle case where LLM returns strings instead of dicts
        valid_attrs = []
        for a in attrs:
            if isinstance(a, dict):
                valid_attrs.append(a)
            elif isinstance(a, str) and ":" in a:
                parts = a.split(":", 1)
                valid_attrs.append({"attribute": parts[0].strip(), "value": parts[1].strip()})
        
        clean = [
            a for a in valid_attrs
            if not any(
                p in str(a.get("attribute","")).lower() + str(a.get("value","")).lower()
                for p in ["price", "$", "usd", "cost"]
            )
        ]
        for i, attr in enumerate(clean[:50]):
            n = i + 1
            if f"ATTRIBUTE_LABEL {n}"  in row: row[f"ATTRIBUTE_LABEL {n}"]  = str(attr.get("attribute",""))
            if f"ATTRIBUTE_VALUE {n}"  in row: row[f"ATTRIBUTE_VALUE {n}"]  = str(attr.get("value",""))
            if f"ATTRIBUTE_UOM {n}"    in row:
                uom = attr.get("uom", "")
                row[f"ATTRIBUTE_UOM {n}"] = str(uom) if uom else ""

    # ---- 6. Actual Image flag ----
    if "Actual Image (Yes/No)" in row:
        row["Actual Image (Yes/No)"] = "Yes" if row.get("Product Image") else "No"

    return row


# ---------------------------------------------------------------------------
# Field coverage
# ---------------------------------------------------------------------------

def count_coverage(row: dict, headers: list) -> dict:
    filled, empty = [], []
    for h in headers:
        (filled if row.get(h, "").strip() else empty).append(h)
    return {
        "fields_filled": len(filled),
        "fields_empty":  len(empty),
        "filled_columns": filled,
        "empty_columns":  empty,
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_csv(path: str, headers: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for row in rows:
            w.writerow([row.get(h, "") for h in headers])
    print(f"[OK] CSV -> {path}  ({len(rows)} rows)")


def write_debug(path: str, records: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    print(f"[OK] Debug -> {path}")


def build_debug_record(
    mpn: str,
    classified: dict,
    ev_stats: dict,
    embed_stats: dict,
    ret_stats: dict,
    llm_stats: dict,
    coverage: dict,
) -> dict:
    pdf_urls = classified.get("pdf_urls", {})
    return {
        "mpn": mpn,
        "discovery": {
            "mfr_url":         classified.get("mfr_url"),
            "ref_urls":        classified.get("ref_urls", []),
            "pdf_found":       sum(len(v) for v in pdf_urls.values()),
            "pdf_by_type":     {k: len(v) for k, v in pdf_urls.items() if v},
            "images_found":    len(classified.get("image_urls", [])),
            "videos":          classified.get("video_urls", []),
            "discarded":       len(classified.get("discard_urls", [])),
        },
        "resource_processing": {
            "html_pages_parsed":  ev_stats.get("html_pages_parsed", 0),
            "pdf_processed":      ev_stats.get("pdf_urls_processed", 0),
            "pdfs_text_based":    ev_stats.get("pdfs_text_based", 0),
            "pdfs_ocr":           ev_stats.get("pdfs_ocr", 0),
            "pdfs_mpn_not_found": ev_stats.get("pdfs_mpn_not_found", 0),
            "pdf_pages_total":    ev_stats.get("pdf_pages_total", 0),
            "pdf_pages_relevant": ev_stats.get("pdf_pages_relevant", 0),
        },
        "evidence": {
            "structured_specs":    ev_stats.get("structured_specs_extracted", 0),
            "structured_fields":   ev_stats.get("structured_evidence_fields", 0),
            "chunks_candidate":    ev_stats.get("chunks_candidate", 0),
            "chunks_created":      ev_stats.get("chunks_created", 0),
            "chunks_skip_struct":  ev_stats.get("chunks_skipped_structured", 0),
            "chunks_skip_irrelevant": ev_stats.get("chunks_skipped_irrelevant", 0),
        },
        "embedding": {
            "chunks_embedded": embed_stats.get("chunks_embedded", 0),
            "chunks_failed":   embed_stats.get("chunks_failed", 0),
            "points_upserted": embed_stats.get("points_upserted", 0),
        },
        "qdrant_retrieval": {
            "groups_queried":    ret_stats.get("groups_queried", 0),
            "chunks_per_group":  ret_stats.get("chunks_per_group", {}),
            "unique_chunks":     ret_stats.get("total_unique_chunks", 0),
            "embed_failures":    ret_stats.get("query_embed_failures", 0),
        },
        "llm": {
            "model":        llm_stats.get("model_used"),
            "prompt_chars": llm_stats.get("prompt_chars", 0),
            "success":      llm_stats.get("success", False),
            "error":        llm_stats.get("error"),
        },
        "field_coverage": {
            "fields_filled": coverage.get("fields_filled", 0),
            "fields_empty":  coverage.get("fields_empty", 0),
            "filled_columns": coverage.get("filled_columns", []),
            "empty_columns":  coverage.get("empty_columns", []),
        },
    }
