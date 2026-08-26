import os
import json
import requests
import time


mpn = "3MABR-7100075678"

with open(f"artifacts/{mpn}/retrieved_evidence.json", "r", encoding="utf-8") as f:
    retrieved_chunks = json.load(f)
    
context = ""
for idx, chunk in enumerate(retrieved_chunks[:10]): # Limit to 10 chunks to avoid 400
    context += f"--- Document {idx+1} (Source: {chunk.get('source_url', 'Unknown')}) ---\n{chunk.get('text', '')}\n\n"
    
prompt = f"""
Product MPN: {mpn}

EVIDENCE:
{context}

Extract specifications from the evidence into this strict JSON schema. If evidence for a field is not found, leave it as an empty string "".
DO NOT guess. Only use facts present in the text.

SCHEMA:
{{
  "MANUFACTURER_NAME": "",
  "BRAND_NAME": "",
  "TRADE_NAME": "",
  "MANUFACTURER_PART_NUMBER": "",
  "ALTERNATE_PART_NUMBER": "",
  "MOBILE_DESC": "",
  "INVOICE_DESC": "",
  "SHORT_DESC": "",
  "LONG_DESC1": "",
  "RETAIL_DESC": "",
  "MARKETING_DESCRIPTION": "",
  "ITEM_FEATURES": ["feature 1", "feature 2"],
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
    {{
      "attribute": "Attribute Name",
      "value": "Value",
      "uom": "Unit of measure if any"
    }}
  ]
}}

CRITICAL INSTRUCTIONS:
1. BE EXHAUSTIVE for dynamic_attributes. Extract EVERY technical specification (e.g. Grain, Substrate, Attachment, Industry, RPM/OPM, Bond, Color, Grade, etc.).
2. DIMENSIONS: Do NOT force circular/cylindrical measurements (like "Diameter = 5 in") into the fixed LENGTH/WIDTH/HEIGHT fields. If the product is a disc/wheel, leave LENGTH/WIDTH/HEIGHT blank and put Diameter inside dynamic_attributes. (e.g. {{"attribute": "Diameter", "value": "5", "uom": "in"}}).
3. PRODUCT-SPECIFIC ONLY: Distinguish between product-specific evidence and product-family evidence. ONLY extract specifications that apply EXACTLY to the requested MPN ({mpn}). Do not extract all sizes/grades mentioned for the broader product family.
4. NO PRICES IN DYNAMIC: Do not put pricing information (List Price, Sale Price) into dynamic_attributes. Use the fixed List_Price field if available.
5. LOOK FOR UNSPSC AND OPM/RPM: Explicitly check the text for UNSPSC codes and Maximum RPM/OPM and extract them if present.
"""

url = "https://api.groq.com/openai/v1/chat/completions"
headers = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json"
}

data = {
    "model": "openai/gpt-oss-120b",
    "messages": [
        {"role": "system", "content": "You are a precise JSON-only product intelligence extractor. Output nothing but valid JSON."},
        {"role": "user", "content": prompt}
    ],
    "temperature": 0.0,
    "response_format": {"type": "json_object"}
}

while True:
    resp = requests.post(url, headers=headers, json=data)
    if resp.status_code == 200:
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        with open(f"artifacts/{mpn}/cpo.json", "w", encoding="utf-8") as f2:
            json.dump(result, f2, indent=2)
        print("Success!")
        break
    elif resp.status_code == 429:
        print("Rate limit... wait 10s")
        time.sleep(10)
    else:
        print("Error:", resp.status_code, resp.text)
        break
