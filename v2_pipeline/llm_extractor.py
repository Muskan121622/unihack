"""
Stage 7 — LLM Extractor
========================
Sends structured_evidence + Qdrant-retrieved chunks to Groq LLM.
Output: filled JSON matching the 252-column schema.

Prompt is two sections:
  SECTION A — STRUCTURED EVIDENCE  (parsed facts, no guessing needed)
  SECTION B — RETRIEVED DOCUMENT EVIDENCE (Qdrant chunks, for remaining fields)

The LLM's job:
  - Route structured facts into the exact schema fields
  - Extract remaining fields from document chunks
  - Never hallucinate — empty string if unsure

Fallback model order (Groq):
  1. llama-3.3-70b-versatile
  2. llama3-70b-8192
  3. mixtral-8x7b-32768
"""

from __future__ import annotations
import os
import re
import json
import time
import requests

BLUESMINDS_API_KEYS = [k.strip() for k in os.getenv("BLUESMINDS_API_KEY", "").split(",") if k.strip()]
_bluesminds_key_idx = 0

# Current Groq models based on actual /v1/models response
_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-oss-20b"
]

def _get_bluesminds_key():
    return BLUESMINDS_API_KEYS[_bluesminds_key_idx] if BLUESMINDS_API_KEYS else ""

def _rotate_bluesminds_key():
    global _bluesminds_key_idx
    if len(BLUESMINDS_API_KEYS) > 1:
        _bluesminds_key_idx = (_bluesminds_key_idx + 1) % len(BLUESMINDS_API_KEYS)
        print(f"    [LLM] Switched to API Key index {_bluesminds_key_idx}")

# ---------------------------------------------------------------------------
# Schema — every field the LLM must fill (maps to the 252-column CSV)
# ---------------------------------------------------------------------------
SCHEMA: dict = {
    "MANUFACTURER_NAME": "",
    "BRAND_NAME": "",
    "TRADE_NAME": "",
    "MANUFACTURER_PART_NUMBER": "",
    "ALTERNATE_PART_NUMBER": "",
    "Classpath": "",
    "MOBILE_DESC": "",
    "INVOICE_DESC": "",
    "SHORT_DESC": "",
    "LONG_DESC1": "",
    "RETAIL_DESC": "",
    "MARKETING_DESCRIPTION": "",
    "ITEM_FEATURES": [],
    "With": "",
    "Standard_Approvals": "",
    "Prop_65": "",
    "Application": "",
    "Includes": "",
    "Product_Name": "",
    "UPC": "",
    "EAN": "",
    "GTIN": "",
    "UNSPSC": "",
    "Warranty": "",
    "List_Price": "",
    "Selling_Qty": "",
    "Selling_UOM": "",
    "Standard_Packaging_Information": "",
    "LENGTH": "",  "LENGTH_UOM": "",
    "HEIGHT": "",  "HEIGHT_UOM": "",
    "WIDTH":  "",  "WIDTH_UOM":  "",
    "WEIGHT": "",  "WEIGHT_UOM": "",
    "VOLUME": "",  "VOLUME_UOM": "",
    "Country_Of_Origin": "",
    "Discontinued": "",
    "dynamic_attributes": [],
    # dynamic_attributes items: {"attribute": str, "value": str, "uom": str}
}

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _structured_section(ev: dict) -> str:
    lines = []

    def add(label: str, value):
        if value and value not in ("", [], None, {}):
            lines.append(f"  {label}: {value}")

    add("MPN",              ev.get("mpn"))
    add("Brand",            ev.get("brand"))
    add("Alternate MPNs",   ", ".join(ev.get("alternate_mpns", [])) or None)
    add("Product Name",     ev.get("product_name"))
    add("Manufacturer",     ev.get("manufacturer_name"))
    add("SKU",              ev.get("sku"))
    add("UPC",              ev.get("UPC"))
    add("EAN",              ev.get("EAN"))
    add("GTIN",             ev.get("GTIN"))
    add("List Price",       ev.get("list_price"))
    add("Warranty",         ev.get("warranty"))
    add("Country of Origin",ev.get("country_of_origin"))

    # Item features
    if ev.get("item_features"):
        lines.append("  Features:")
        for f in ev["item_features"][:20]:
            lines.append(f"    - {f}")

    # Parsed specs (key: value from HTML tables + PDF KV)
    specs = ev.get("parsed_specs", {})
    if specs:
        lines.append("  --- Parsed Specifications ---")
        for k, v in list(specs.items())[:100]:
            lines.append(f"  {k}: {v}")

    return "\n".join(lines) or "  (none)"


def _chunks_section(chunks: list) -> str:
    if not chunks:
        return "  (no document chunks retrieved)"
    parts = []
    for i, c in enumerate(chunks, 1):
        src  = c.get("source_type", "?")
        url  = c.get("source_url", "")[:60]
        text = c.get("text", "").strip()[:900]
        parts.append(f"  [Chunk {i} | {src} | {url}]\n  {text}")
    return "\n\n".join(parts)


def _build_prompt(mpn: str, brand: str, part_desc: str,
                  ev: dict, chunks: list) -> str:
    return f"""You are a product data specialist. Your ONLY job is to populate the JSON schema below using the evidence provided.

PRODUCT IDENTITY
  MPN         : {mpn}
  Brand       : {brand}
  Input Desc  : {part_desc}

===== SECTION A — STRUCTURED EVIDENCE (parsed from product pages / PDFs) =====
{_structured_section(ev)}

===== SECTION B — RETRIEVED DOCUMENT EVIDENCE (Qdrant semantic chunks) =====
{_chunks_section(chunks)}

===== OUTPUT INSTRUCTIONS =====
Fill the JSON schema below. Rules:
1. Use ONLY facts from SECTION A or B. Do NOT guess or hallucinate.
2. If evidence for a field is absent → leave it as "" or [].
3. For ITEM_FEATURES: bullet points from SECTION A "Features" list (max 20).
4. For dynamic_attributes: extract EVERY distinct technical spec, measurement,
   material, grade, performance metric, packaging detail as a separate entry.
   Examples: Belt Width, Belt Length, Grit, Voltage, Amperage, Diameter,
   Pack Size, Sound Level, Mounting Type, Number of Wash Cycles, etc.
5. Dimensions → put numeric value in LENGTH/HEIGHT/WIDTH, unit in _UOM field.
   Example: LENGTH="18", LENGTH_UOM="in"
6. Description fields:
   MOBILE_DESC         → ≤60 chars: brand + key spec summary
   INVOICE_DESC        → ≤80 chars: brand + product type + key spec
   SHORT_DESC          → 1-sentence product summary
   LONG_DESC1          → 2–4 sentence rich description
   RETAIL_DESC         → Customer-friendly short sentence
   MARKETING_DESCRIPTION → Full marketing paragraph
7. Standard_Approvals → pipe-separated list: "UL Listed|CE|RoHS"
8. Output ONLY the JSON. No markdown. No explanation.

SCHEMA:
{json.dumps(SCHEMA, indent=2)}
"""


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def extract_schema(
    mpn: str,
    brand: str,
    part_desc: str,
    ev: dict,
    chunks: list,
) -> tuple[dict, dict]:
    """Call BluesMinds LLM to fill the schema."""
    prompt = _build_prompt(mpn, brand, part_desc, ev, chunks)
    stats  = {
        "prompt_chars": len(prompt),
        "model_used":   None,
        "models_tried": [],
        "success":      False,
        "error":        None,
    }

    if not BLUESMINDS_API_KEYS:
        stats["error"] = "BLUESMINDS_API_KEY not set"
        return {}, stats

    print(f"    [LLM] Prompt {stats['prompt_chars']} chars | {len(chunks)} chunks")

    for model in _MODELS:
        stats["models_tried"].append(model)
        print(f"    [LLM] Trying {model}")
        
        max_attempts_per_model = len(BLUESMINDS_API_KEYS) + 1
        
        for attempt in range(max_attempts_per_model):
            api_key = _get_bluesminds_key()
            url = "https://api.bluesminds.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            
            payload = {
                "model":           model,
                "messages":        [
                    {"role": "system", "content": "You are a precise JSON-only product data extractor. Output ONLY valid JSON, starting with { and ending with }."},
                    {"role": "user", "content": prompt},
                ],
                "temperature":     0.0,
                "response_format": {"type": "json_object"},
            }
            
            try:
                import requests, time, json
                r = requests.post(url, headers=headers, json=payload, timeout=120)
            except Exception as e:
                print(f"    [LLM] Request exception: {e}")
                stats["error"] = str(e)
                _rotate_bluesminds_key()
                time.sleep(2)
                continue
    
            if r.status_code == 200:
                try:
                    raw = r.json()["choices"][0]["message"]["content"]
                    raw = re.sub(r"^`(?:json)?\\s*", "", raw.strip())
                    raw = re.sub(r"\\s*`$", "", raw)
                    result = json.loads(raw)
                    stats["model_used"] = model
                    stats["success"]    = True
                    print(f"    [LLM] Success with {model}")
                    return result, stats
                except Exception as e:
                    print(f"    [LLM] JSON parse or response error ({model}): {e}")
                    stats["error"] = f"JSON/Parse: {e}"
                    break
            elif r.status_code == 413:
                print(f"    [LLM] Payload too large for {model}")
                break
            elif r.status_code == 429:
                err_msg = r.json().get("error", {}).get("message", "") if r.text else ""
                print(f"    [LLM] 429 Rate Limit on BluesMinds: {err_msg[:100]}")
                _rotate_bluesminds_key()
                time.sleep(1)
                if attempt == len(BLUESMINDS_API_KEYS) - 1:
                    print(f"    [LLM] All keys exhausted for this attempt. Waiting 75s...")
                    time.sleep(75)
            else:
                try:
                    err = r.json().get("error", {}).get("message", r.text[:150])
                except:
                    err = r.text[:150] if hasattr(r, "text") and r.text else "No error text"
                print(f"    [LLM] HTTP {r.status_code}: {err}")
                stats["error"] = f"HTTP {r.status_code}: {err}"
                
                if r.status_code == 400:
                    break # Model failed to output JSON, move to next model!
                elif r.status_code in [401, 403, 500, 503]:
                    _rotate_bluesminds_key()
                    time.sleep(1)
                else:
                    time.sleep(5)
                    break

    print(f"    [LLM] All models/keys failed for {mpn}")
    return {}, stats


# ---------------------------------------------------------------------------
# Pre-fill: put known structured facts directly into the schema dict
# so the LLM only needs to confirm / fill gaps
# ---------------------------------------------------------------------------

def pre_fill(schema: dict, ev: dict) -> dict:
    """Inject structured evidence into schema. LLM output can override."""
    out = dict(schema)

    def _set(field, val):
        if val and not out.get(field):
            out[field] = val

    _set("MANUFACTURER_PART_NUMBER", ev.get("mpn"))
    _set("ALTERNATE_PART_NUMBER",
         ", ".join(ev.get("alternate_mpns", [])) if ev.get("alternate_mpns") else "")
    _set("BRAND_NAME",       ev.get("brand"))
    _set("Product_Name",     ev.get("product_name"))
    _set("UPC",              ev.get("UPC"))
    _set("EAN",              ev.get("EAN"))
    _set("GTIN",             ev.get("GTIN"))
    _set("List_Price",       ev.get("list_price"))
    _set("Country_Of_Origin",ev.get("country_of_origin"))
    _set("Warranty",         ev.get("warranty"))
    _set("MANUFACTURER_NAME",ev.get("manufacturer_name"))

    # Item features
    if ev.get("item_features") and not out.get("ITEM_FEATURES"):
        out["ITEM_FEATURES"] = ev["item_features"]

    return out
