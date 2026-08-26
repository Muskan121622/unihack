"""
Stage 8: CSV Writer + Evidence Debug
=====================================
Writes two files for the 5-row test:
  1. final_delivery_5_rows.csv — exact 252-column schema
  2. evidence_debug_5_rows.json — per-product pipeline stats for inspection

The CSV writer:
  - Reads template headers from Unihack_ Expected Output - Delivery Format.csv
  - Fills fields from both structured evidence AND LLM output
  - Validates resource URLs (rejects anchors, category pages, non-PDF doc columns)
  - Normalizes dynamic_attributes → ATTRIBUTE_LABEL n / ATTRIBUTE_VALUE n / ATTRIBUTE_UOM n
  - Normalizes ITEM_FEATURES → ITEM_FEATURES_1 ... ITEM_FEATURES_20

Evidence debug JSON captures everything from the pipeline run for every product,
so we can answer: which fields are filled, which are empty, and why.
"""

import os
import re
import csv
import json


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _is_valid_document_url(url: str) -> bool:
    """
    Return True only if a URL looks like a real document
    (PDF, or known doc host) — not an anchor link or category page.
    """
    if not url:
        return False
    url_lower = url.lower()
    # Reject anchor-only suffixes on non-PDF URLs
    if "#" in url and ".pdf" not in url_lower:
        return False
    # Reject category/search pages
    bad_patterns = [
        "/catalogsearch/", "/search?", "/category/", "/sales-specials",
        "sales-specials", "/c-", "?filters=", "/browse/",
    ]
    if any(p in url_lower for p in bad_patterns):
        return False
    # Must be PDF or a direct doc path
    is_doc = (
        ".pdf" in url_lower
        or "/pdf/" in url_lower
        or "/document/" in url_lower
        or "/manuals/" in url_lower
        or "/downloads/" in url_lower
        or "download=true" in url_lower
    )
    return is_doc


def _is_valid_image_url(url: str) -> bool:
    if not url:
        return False
    url_lower = url.lower()
    image_exts = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]
    return any(url_lower.split("?")[0].endswith(ext) for ext in image_exts)


def _is_valid_video_url(url: str) -> bool:
    if not url:
        return False
    url_lower = url.lower()
    return (
        "youtube.com/watch" in url_lower
        or "youtu.be/" in url_lower
        or (re.search(r"vimeo\.com/\d+", url_lower) is not None)
    )


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def build_csv_row(
    template_headers: list,
    input_row: dict,
    structured: dict,
    llm_output: dict,
) -> dict:
    """
    Build the complete CSV row dict from structured evidence + LLM output.

    Priority:
      1. Input CSV passthrough fields (MPN, brand columns, etc.)
      2. Structured evidence (URLs, images, barcodes)
      3. LLM-filled fields
    """
    row = {h: "" for h in template_headers}

    # --- Passthrough from input CSV ---
    for col in ["Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand",
                "DIB_Brand", "Part_Manuf", "PART_NUMBER", "Dept", "Class",
                "Fine", "SKU - MY_PART_NUMBER"]:
        if col in row:
            row[col] = input_row.get(col, "")

    # --- URL columns from structured evidence ---
    # MFR URL
    if "MFR URL" in row:
        row["MFR URL"] = structured.get("mfr_url") or ""

    # Ref URLs 1–5
    ref_urls = structured.get("ref_urls", [])
    for i, url in enumerate(ref_urls[:5]):
        col = f"Ref URL {i+1}"
        if col in row:
            row[col] = url

    # --- Image columns ---
    prod_img = structured.get("product_image")
    if prod_img and _is_valid_image_url(prod_img):
        if "Product Image" in row:
            row["Product Image"] = prod_img

    alt_imgs = structured.get("alt_images", [])
    for i, img in enumerate(alt_imgs[:4]):
        col = f"Alternate Image {i+1}"
        if col in row and _is_valid_image_url(img):
            row[col] = img

    # --- Document URL columns ---
    doc_col_map = {
        "SDS":                        "sds_url",
        "SDS_1":                      "sds_1_url",
        "Catalog":                    "catalog_url",
        "Specification Sheet":        "specification_url",
        "Instruction/Installation Manual": "manual_url",
        "Service Manual":             "service_manual_url",
        "Owners/User Manual":         "owners_manual_url",
    }
    for csv_col, evidence_key in doc_col_map.items():
        if csv_col in row:
            val = structured.get(evidence_key)
            if val and _is_valid_document_url(val):
                row[csv_col] = val

    # Video
    if "Video Link" in row:
        vid = structured.get("video_url")
        if vid and _is_valid_video_url(vid):
            row["Video Link"] = vid

    # --- LLM output: fixed fields ---
    llm_field_map = {
        "MANUFACTURER_NAME":    "MANUFACTURER_NAME",
        "BRAND_NAME":           "BRAND_NAME",
        "TRADE_NAME":           "TRADE_NAME",
        "MANUFACTURER_PART_NUMBER": "MANUFACTURER_PART_NUMBER",
        "ALTERNATE_PART_NUMBER": "ALTERNATE_PART_NUMBER",
        "Classpath":            "Classpath",
        "MOBILE_DESC":          "MOBILE_DESC",
        "INVOICE_DESC":         "INVOICE_DESC",
        "SHORT_DESC":           "SHORT_DESC",
        "LONG_DESC1":           "LONG_DESC1",
        "RETAIL_DESC":          "RETAIL_DESC",
        "MARKETING_DESCRIPTION": "MARKETING_DESCRIPTION",
        "With":                 "With",
        "Standard_Approvals":   "Standard/Approvals",
        "Prop_65":              "Prop 65",
        "Application":          "Application",
        "Includes":             "Includes",
        "Product_Name":         "Product Name",
        "UPC":                  "UPC",
        "EAN":                  "EAN",
        "GTIN":                 "GTIN",
        "UNSPSC":               "UNSPSC",
        "Warranty":             "Warranty",
        "List_Price":           "List Price",
        "Selling_Qty":          "Selling Qty",
        "Selling_UOM":          "Selling UOM",
        "Standard_Packaging_Information": "Standard Packaging Information",
        "LENGTH":               "LENGTH",
        "LENGTH_UOM":           "LENGTH_UOM",
        "HEIGHT":               "HEIGHT",
        "HEIGHT_UOM":           "HEIGHT_UOM",
        "WIDTH":                "WIDTH",
        "WIDTH_UOM":            "WIDTH_UOM",
        "WEIGHT":               "WEIGHT",
        "WEIGHT_UOM":           "WEIGHT_UOM",
        "VOLUME":               "VOLUME",
        "VOLUME_UOM":           "VOLUME_UOM",
        "Country_Of_Origin":    "Country Of Origin",
        "Discontinued":         "Discontinued",
    }
    for llm_key, csv_col in llm_field_map.items():
        if csv_col in row:
            val = llm_output.get(llm_key, "")
            if val and val != "" and not isinstance(val, (list, dict)):
                row[csv_col] = str(val)

    # Override barcodes with structured evidence if LLM missed them
    for bc_key, csv_col in [("UPC", "UPC"), ("EAN", "EAN"), ("GTIN", "GTIN")]:
        if not row.get(csv_col) and structured.get(bc_key):
            row[csv_col] = structured[bc_key]

    # --- ITEM_FEATURES ---
    features = llm_output.get("ITEM_FEATURES", [])
    if isinstance(features, list):
        for i, feat in enumerate(features[:20]):
            col = f"ITEM_FEATURES_{i+1}"
            if col in row:
                row[col] = str(feat)

    # --- Dynamic attributes → ATTRIBUTE_LABEL/VALUE/UOM ---
    dyn_attrs = llm_output.get("dynamic_attributes", [])
    if isinstance(dyn_attrs, list):
        # Filter out pricing attributes
        filtered = []
        for attr in dyn_attrs:
            lbl = str(attr.get("attribute", "")).lower()
            val = str(attr.get("value", "")).lower()
            if any(p in lbl or p in val for p in ["price", "$", "usd", "cost"]):
                continue
            filtered.append(attr)

        for i, attr in enumerate(filtered[:50]):
            n = i + 1
            if f"ATTRIBUTE_LABEL {n}" in row:
                row[f"ATTRIBUTE_LABEL {n}"] = str(attr.get("attribute", ""))
            if f"ATTRIBUTE_VALUE {n}" in row:
                row[f"ATTRIBUTE_VALUE {n}"] = str(attr.get("value", ""))
            if f"ATTRIBUTE_UOM {n}" in row:
                uom = attr.get("uom", "")
                row[f"ATTRIBUTE_UOM {n}"] = str(uom) if uom else ""

    # Actual Image (Yes/No) flag
    if "Actual Image (Yes/No)" in row:
        row["Actual Image (Yes/No)"] = "Yes" if row.get("Product Image") else "No"

    return row


# ---------------------------------------------------------------------------
# Field coverage counting
# ---------------------------------------------------------------------------

def count_field_coverage(row: dict, template_headers: list) -> dict:
    """Count filled vs empty fields in the output row."""
    filled = 0
    empty = 0
    filled_cols = []
    empty_cols = []

    for col in template_headers:
        val = row.get(col, "")
        if val and str(val).strip():
            filled += 1
            filled_cols.append(col)
        else:
            empty += 1
            empty_cols.append(col)

    return {
        "fields_filled": filled,
        "fields_empty": empty,
        "filled_columns": filled_cols,
        "empty_columns": empty_cols,
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_csv(output_path: str, template_headers: list, rows: list) -> None:
    """Write the final 252-column CSV."""
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(template_headers)
        for row in rows:
            writer.writerow([row.get(h, "") for h in template_headers])
    print(f"[✓] CSV written: {output_path} ({len(rows)} rows)")


def write_evidence_debug(output_path: str, debug_records: list) -> None:
    """Write the evidence debug JSON."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(debug_records, f, indent=2, ensure_ascii=False)
    print(f"[✓] Evidence debug written: {output_path}")


def build_evidence_debug_record(
    mpn: str,
    classified_resources: dict,
    evidence_stats: dict,
    embedding_stats: dict,
    retrieval_stats: dict,
    llm_stats: dict,
    field_coverage: dict,
) -> dict:
    """
    Build the per-product evidence debug record.
    This is what we inspect to understand pipeline performance.
    """
    pdf_urls = classified_resources.get("pdf_urls", {})
    pdf_total = sum(len(v) for v in pdf_urls.values())

    return {
        "mpn": mpn,

        "discovery": {
            "mfr_url": classified_resources.get("mfr_url"),
            "ref_urls_count": len(classified_resources.get("ref_urls", [])),
            "ref_urls": classified_resources.get("ref_urls", []),
            "pdf_urls_found": pdf_total,
            "pdf_by_type": {k: len(v) for k, v in pdf_urls.items() if v},
            "image_urls_found": len(classified_resources.get("image_urls", [])),
            "video_urls": classified_resources.get("video_urls", []),
            "discard_urls_count": len(classified_resources.get("discard_urls", [])),
        },

        "resource_processing": {
            "html_pages_parsed":      evidence_stats.get("html_pages_parsed", 0),
            "pdf_urls_processed":     evidence_stats.get("pdf_urls_processed", 0),
            "pdfs_text_based":        evidence_stats.get("pdfs_text_based", 0),
            "pdfs_ocr":               evidence_stats.get("pdfs_ocr", 0),
            "pdfs_mpn_not_found":     evidence_stats.get("pdfs_mpn_not_found", 0),
            "pdf_pages_total":        evidence_stats.get("pdf_pages_total", 0),
            "pdf_pages_relevant":     evidence_stats.get("pdf_pages_relevant", 0),
        },

        "evidence": {
            "structured_specs_extracted": evidence_stats.get("structured_specs_extracted", 0),
            "structured_evidence_fields": evidence_stats.get("structured_evidence_fields", 0),
            "chunks_candidate":           evidence_stats.get("chunks_candidate", 0),
            "chunks_created":             evidence_stats.get("chunks_created", 0),
            "chunks_skipped_structured":  evidence_stats.get("chunks_skipped_structured", 0),
            "chunks_skipped_irrelevant":  evidence_stats.get("chunks_skipped_irrelevant", 0),
        },

        "embedding": {
            "chunks_embedded":  embedding_stats.get("chunks_embedded", 0),
            "chunks_failed":    embedding_stats.get("chunks_failed", 0),
            "points_upserted":  embedding_stats.get("points_upserted", 0),
        },

        "qdrant_retrieval": {
            "groups_queried":       retrieval_stats.get("groups_queried", 0),
            "chunks_per_group":     retrieval_stats.get("chunks_per_group", {}),
            "total_unique_chunks":  retrieval_stats.get("total_unique_chunks", 0),
            "embed_failures":       retrieval_stats.get("query_embed_failures", 0),
        },

        "llm_extraction": {
            "model_used":       llm_stats.get("model_used"),
            "prompt_chars":     llm_stats.get("prompt_chars", 0),
            "success":          llm_stats.get("success", False),
            "error":            llm_stats.get("error"),
        },

        "field_coverage": {
            "fields_filled": field_coverage.get("fields_filled", 0),
            "fields_empty":  field_coverage.get("fields_empty", 0),
            "filled_columns": field_coverage.get("filled_columns", []),
            "empty_columns":  field_coverage.get("empty_columns", []),
        },
    }
