"""
Stage 7: LLM Extractor
=======================
Sends BOTH structured evidence AND Qdrant-retrieved chunks to Groq LLM.
The LLM fills the 252-column schema JSON.

Prompt structure:
  PRODUCT: {MPN}
  Brand: {brand}

  STRUCTURED EVIDENCE (parsed from product pages / PDFs):
  {key: value pairs — already known facts}

  RETRIEVED DOCUMENT EVIDENCE:
  [chunk 1 — source type]
  ...text...

  Fill the schema. Only use facts from the evidence above. Never guess.

Models tried in fallback order (Groq):
  1. llama-3.3-70b-versatile   (most capable, best JSON)
  2. llama3-70b-8192            (fallback)
  3. mixtral-8x7b-32768         (fallback)
"""

import os
import re
import json
import time
import requests

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MODELS_TO_TRY = [
    "llama-3.3-70b-versatile",
    "llama3-70b-8192",
    "mixtral-8x7b-32768",
]

# The full 252-column schema as a JSON template
# (field names match the CSV headers exactly)
SCHEMA_TEMPLATE = {
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
    "ITEM_FEATURES": [],          # up to 20
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
    "LENGTH": "", "LENGTH_UOM": "",
    "HEIGHT": "", "HEIGHT_UOM": "",
    "WIDTH": "", "WIDTH_UOM": "",
    "WEIGHT": "", "WEIGHT_UOM": "",
    "VOLUME": "", "VOLUME_UOM": "",
    "Country_Of_Origin": "",
    "Discontinued": "",
    "dynamic_attributes": [
        # {"attribute": "...", "value": "...", "uom": "..."}
    ],
}


def _build_structured_evidence_section(structured: dict) -> str:
    """Format the structured evidence for the LLM prompt."""
    lines = []

    def add(label, value):
        if value and value != "" and value != [] and value is not None:
            lines.append(f"  {label}: {value}")

    add("MPN", structured.get("mpn"))
    add("Brand", structured.get("brand"))
    add("Alternate MPNs", ", ".join(structured.get("alternate_mpns", [])) or None)
    add("Product Name", structured.get("product_name"))
    add("SKU", structured.get("sku"))
    add("UPC", structured.get("UPC"))
    add("EAN", structured.get("EAN"))
    add("GTIN", structured.get("GTIN"))
    add("List Price", structured.get("list_price"))
    add("Country of Origin", structured.get("country_of_origin"))

    # Parsed specs (key: value table)
    specs = structured.get("parsed_specs", {})
    if specs:
        lines.append("  --- Parsed Specifications ---")
        for k, v in list(specs.items())[:80]:  # cap at 80 spec rows to stay within context
            lines.append(f"  {k}: {v}")

    return "\n".join(lines) if lines else "  (none)"


def _build_retrieved_chunks_section(chunks: list) -> str:
    """Format Qdrant-retrieved chunks for the LLM prompt."""
    if not chunks:
        return "  (no additional document evidence retrieved)"

    parts = []
    for i, chunk in enumerate(chunks, 1):
        src_type = chunk.get("source_type", "unknown")
        src_url = chunk.get("source_url", "")
        text = chunk.get("text", "").strip()
        parts.append(
            f"  [Chunk {i} | {src_type} | {src_url[:60]}]\n"
            f"  {text[:800]}"  # cap each chunk at 800 chars to control context
        )
    return "\n\n".join(parts)


def _build_prompt(mpn: str, brand: str, part_desc: str,
                  structured: dict, chunks: list) -> str:
    structured_section = _build_structured_evidence_section(structured)
    chunks_section = _build_retrieved_chunks_section(chunks)

    return f"""You are a product data specialist. Extract information from the provided evidence and fill the JSON schema.

PRODUCT: {mpn}
Brand: {brand}
Input Description: {part_desc}

========== STRUCTURED EVIDENCE (parsed from product pages / PDFs) ==========
{structured_section}

========== RETRIEVED DOCUMENT EVIDENCE ==========
{chunks_section}

========== INSTRUCTIONS ==========
Fill the JSON schema below using ONLY the facts from the evidence above.
DO NOT guess. DO NOT hallucinate values.
If a field has no evidence, leave it as "" (empty string) or [] (empty list).

For dynamic_attributes: Extract EVERY distinct technical specification, measurement,
material, grade, performance metric, packaging detail as a separate entry.
Examples: Grit, Belt Width, Belt Length, Backing Material, OPM, Diameter, Pack Size, etc.

For ITEM_FEATURES: Extract distinct marketing/feature bullet points (max 20).

For dimensions (LENGTH/HEIGHT/WIDTH/WEIGHT/VOLUME): Put only the numeric value in the field,
the unit in the _UOM field. Example: LENGTH="18", LENGTH_UOM="in"

For description fields:
  MOBILE_DESC = Very short (< 60 chars): brand + key specs
  INVOICE_DESC = Short (< 80 chars): brand + product type + key spec
  SHORT_DESC = 1 sentence product summary
  LONG_DESC1 = Full rich description (2-4 sentences)
  RETAIL_DESC = Customer-friendly marketing sentence
  MARKETING_DESCRIPTION = Full marketing paragraph

SCHEMA (fill this exactly):
{json.dumps(SCHEMA_TEMPLATE, indent=2)}
"""


def extract_schema(
    mpn: str,
    brand: str,
    part_desc: str,
    structured: dict,
    chunks: list,
) -> tuple[dict, dict]:
    """
    Call Groq LLM to fill the 252-column schema.

    Returns:
        (filled_schema_dict, extraction_stats)
    """
    prompt = _build_prompt(mpn, brand, part_desc, structured, chunks)
    prompt_chars = len(prompt)
    print(f"    [LLM] Prompt size: {prompt_chars} chars | Chunks in context: {len(chunks)}")

    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY not set. Please set the environment variable.")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    stats = {
        "prompt_chars": prompt_chars,
        "model_used": None,
        "models_tried": [],
        "success": False,
        "error": None,
    }

    for model_name in MODELS_TO_TRY:
        stats["models_tried"].append(model_name)
        print(f"    [LLM] Trying model: {model_name}")

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise JSON-only product data extractor. "
                        "Output ONLY valid JSON matching the provided schema. "
                        "No markdown. No explanation. No extra keys."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "max_tokens": 8192,
        }

        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=120)
        except Exception as e:
            print(f"    [LLM] Request exception: {e}")
            stats["error"] = str(e)
            time.sleep(5)
            continue

        if resp.status_code == 200:
            raw_content = resp.json()["choices"][0]["message"]["content"]
            # Strip any accidental markdown fences
            clean = re.sub(r"^```(?:json)?\s*", "", raw_content.strip())
            clean = re.sub(r"\s*```$", "", clean)
            try:
                result = json.loads(clean)
                stats["model_used"] = model_name
                stats["success"] = True
                print(f"    [LLM] Success with {model_name}")
                return result, stats
            except json.JSONDecodeError as e:
                print(f"    [LLM] JSON parse error with {model_name}: {e}")
                stats["error"] = f"JSON parse: {e}"
                continue

        elif resp.status_code == 413:
            print(f"    [LLM] Payload too large for {model_name} — trying next model")
        elif resp.status_code == 429:
            wait = 30
            print(f"    [LLM] Rate limit (429) — waiting {wait}s")
            time.sleep(wait)
        elif resp.status_code == 400:
            err = resp.json().get("error", {}).get("message", "")
            print(f"    [LLM] 400 error with {model_name}: {err}")
            stats["error"] = err
            # Context too long or model not available — try next
        else:
            print(f"    [LLM] {resp.status_code} error with {model_name}: {resp.text[:200]}")
            stats["error"] = f"HTTP {resp.status_code}"
            time.sleep(5)

    print(f"    [LLM] All models failed for {mpn}")
    return {}, stats


def pre_fill_from_structured(schema: dict, structured: dict) -> dict:
    """
    Pre-fill known schema fields directly from structured evidence
    before sending to LLM. This means the LLM only needs to confirm
    or fill the gaps — not derive everything from scratch.

    Returns the schema with pre-filled values (LLM can override these).
    """
    pre = dict(schema)

    # Identity
    if not pre.get("MANUFACTURER_PART_NUMBER"):
        pre["MANUFACTURER_PART_NUMBER"] = structured.get("mpn", "")
    if not pre.get("ALTERNATE_PART_NUMBER"):
        pre["ALTERNATE_PART_NUMBER"] = ", ".join(structured.get("alternate_mpns", []))
    if not pre.get("BRAND_NAME"):
        pre["BRAND_NAME"] = structured.get("brand", "")
    if not pre.get("Product_Name"):
        pre["Product_Name"] = structured.get("product_name", "")

    # Barcodes
    if not pre.get("UPC"):
        pre["UPC"] = structured.get("UPC", "")
    if not pre.get("EAN"):
        pre["EAN"] = structured.get("EAN", "")
    if not pre.get("GTIN"):
        pre["GTIN"] = structured.get("GTIN", "")

    # Price
    if not pre.get("List_Price"):
        pre["List_Price"] = structured.get("list_price", "")

    # Country
    if not pre.get("Country_Of_Origin"):
        pre["Country_Of_Origin"] = structured.get("country_of_origin", "")

    return pre
