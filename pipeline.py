import os
import json
import csv
import requests
import time
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, VectorParams, Distance

VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

def embed_query(text):
    url = "https://api.voyageai.com/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {VOYAGE_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "input": [text],
        "model": "voyage-3",
        "input_type": "query"
    }
    for attempt in range(5):
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code == 200:
            return resp.json()["data"][0]["embedding"]
        elif resp.status_code == 429:
            print(f"    [!] Pipeline Voyage Rate Limit. Waiting 65s..."); time.sleep(65)
    return None

def retrieve(client, mpn, query):
    print(f"[*] Retrieving evidence for {mpn}...")
    vector = embed_query(query)
    if not vector:
        return []
        
    results = client.query_points(
        collection_name="evidence",
        query=vector,
        query_filter=Filter(
            must=[FieldCondition(key="mpn", match=MatchValue(value=mpn))]
        ),
        limit=15
    )
    return [r.payload for r in results.points]

def extract_facts(mpn, part_desc, retrieved_chunks):
    print("[*] Extracting facts using Groq LLM...")
    
    context = ""
    for idx, chunk in enumerate(retrieved_chunks):
        context += f"--- Document {idx+1} (Source: {chunk.get('source_url', 'Unknown')}) ---\n{chunk.get('text', '')}\n\n"
        
    prompt = f"""
    Product MPN: {mpn}
    Description: {part_desc}
    
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
    
    CRITICAL INSTRUCTION FOR dynamic_attributes:
    BE EXHAUSTIVE. Extract EVERY technical specification, measurement, performance metric, application, substrate, material, coating, grade, speed (OPM), construction detail, and packaging detail mentioned in the evidence as a separate dynamic attribute. 
    For example, if the evidence mentions "Mineral: Ceramic Aluminum Oxide", "Maximum OPM: 15,000", "3 mil film backing", create distinct dynamic attributes for all of them.
    Do not skip anything.
    """
    
    models_to_try = [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b", 
        "qwen/qwen3.6-27b",
        "llama-3.3-70b-versatile"
    ]
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    for model_name in models_to_try:
        data = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "You are a precise JSON-only product intelligence extractor. Output nothing but valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }
        
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            content = content.replace('`json', '').replace('`', '').strip()
            try:
                result = json.loads(content)
                print(f"[*] Success with model: {model_name}")
                return result
            except:
                continue
        elif resp.status_code == 413 and model_name == "openai/gpt-oss-120b":
            print("[-] 120b payload too large, failing over...")
        else:
            print(f"[-] Model {model_name} failed: {resp.status_code}")
            
    print("[-] All models failed or returned invalid JSON.")
    return {}


def validate_and_normalize(cpo_data, resources, part_desc):
    print("[*] Validating and normalizing extracted data...")
    desc_lower = part_desc.lower()
    is_circular = any(x in desc_lower for x in ["disc", "wheel", "pad", "roll"])
    
    # 1. Dimension Semantics
    if is_circular:
        length = cpo_data.get("LENGTH", "")
        width = cpo_data.get("WIDTH", "")
        
        # If length or width exists, it's likely a diameter for circular objects
        if length or width:
            diam = length if length else width
            uom = cpo_data.get("LENGTH_UOM", "") or cpo_data.get("WIDTH_UOM", "")
            
            # Clear them from fixed columns
            cpo_data["LENGTH"] = ""
            cpo_data["LENGTH_UOM"] = ""
            cpo_data["WIDTH"] = ""
            cpo_data["WIDTH_UOM"] = ""
            
            # Push to dynamic attributes
            if "dynamic_attributes" not in cpo_data:
                cpo_data["dynamic_attributes"] = []
            
            # check if diameter is already there
            has_diam = any("diameter" in d.get("attribute", "").lower() for d in cpo_data["dynamic_attributes"])
            if not has_diam:
                cpo_data["dynamic_attributes"].append({
                    "attribute": "Diameter",
                    "value": diam,
                    "uom": uom
                })
                
    # 2. Filter Pricing from dynamic attributes
    if "dynamic_attributes" in cpo_data:
        filtered_attrs = []
        for attr in cpo_data["dynamic_attributes"]:
            lbl = attr.get("attribute", "").lower()
            val = attr.get("value", "").lower()
            if "price" in lbl or "usd" in lbl or "price" in val or "$" in val:
                print(f"    [-] Filtering out pricing attribute: {attr}")
                continue
            filtered_attrs.append(attr)
        cpo_data["dynamic_attributes"] = filtered_attrs
        
    # 3. Resource Relevance (Structural URL validation)
    validated_resources = {}
    for res_name, res_url in resources.items():
        if res_name in ["SDS", "Specification Sheet", "Catalog"]:
            url_lower = res_url.lower()
            if ".pdf" in url_lower or "/doc/" in url_lower:
                if "/catalogsearch/" not in url_lower and "aspx" not in url_lower.split("?")[-1]:
                    validated_resources[res_name] = res_url
                else:
                    print(f"    [-] Invalid structural resource URL dropped: {res_url}")
            else:
                print(f"    [-] Non-document URL dropped for {res_name}: {res_url}")
        else:
            validated_resources[res_name] = res_url
            
    return cpo_data, validated_resources

def generate_output(cpo_data, output_dir, input_row, template_headers, discovery_urls, resources):


    mpn = input_row.get("Mfg_Part_Num", "")
    part_desc = input_row.get("Part_Desc", "")
    
    cpo_data, resources = validate_and_normalize(cpo_data, resources, part_desc)
        
    row_dict = {h: "" for h in template_headers}
    row_dict["Mfg_Part_Num"] = mpn
    row_dict["Part_Desc"] = input_row.get("Part_Desc", "")
    row_dict["E1_Brand"] = input_row.get("E1_Brand", "")
    row_dict["Unilog_Brand"] = input_row.get("Unilog_Brand", "")
    row_dict["DIB_Brand"] = input_row.get("DIB_Brand", "")
    row_dict["Part_Manuf"] = input_row.get("Part_Manuf", "")
    
    for i, url in enumerate(discovery_urls[:5]):
        if i == 0: row_dict["Ref URL 1"] = url
        else: row_dict[f"Ref URL {i+1}"] = url
            
    fields_mapping = {
        "MANUFACTURER_NAME": "MANUFACTURER_NAME",
        "BRAND_NAME": "BRAND_NAME",
        "TRADE_NAME": "TRADE_NAME",
        "MANUFACTURER_PART_NUMBER": "MANUFACTURER_PART_NUMBER",
        "ALTERNATE_PART_NUMBER": "ALTERNATE_PART_NUMBER",
        "MOBILE_DESC": "MOBILE_DESC",
        "INVOICE_DESC": "INVOICE_DESC",
        "SHORT_DESC": "SHORT_DESC",
        "LONG_DESC1": "LONG_DESC1",
        "RETAIL_DESC": "RETAIL_DESC",
        "MARKETING_DESCRIPTION": "MARKETING_DESCRIPTION",
        "With": "With",
        "Standard_Approvals": "Standard/Approvals",
        "Prop_65": "Prop 65",
        "Application": "Application",
        "Includes": "Includes",
        "Product_Name": "Product Name",
        "UPC": "UPC",
        "EAN": "EAN",
        "GTIN": "GTIN",
        "UNSPSC": "UNSPSC",
        "Warranty": "Warranty",
        "List_Price": "List Price",
        "Selling_Qty": "Selling Qty",
        "Selling_UOM": "Selling UOM",
        "Standard_Packaging_Information": "Standard Packaging Information",
        "LENGTH": "LENGTH",
        "LENGTH_UOM": "LENGTH_UOM",
        "HEIGHT": "HEIGHT",
        "HEIGHT_UOM": "HEIGHT_UOM",
        "WIDTH": "WIDTH",
        "WIDTH_UOM": "WIDTH_UOM",
        "WEIGHT": "WEIGHT",
        "WEIGHT_UOM": "WEIGHT_UOM",
        "VOLUME": "VOLUME",
        "VOLUME_UOM": "VOLUME_UOM",
        "Country_Of_Origin": "Country Of Origin",
        "Discontinued": "Discontinued"
    }
    
    for json_key, csv_col in fields_mapping.items():
        if csv_col in row_dict:
            row_dict[csv_col] = cpo_data.get(json_key, "")
            
    features = cpo_data.get("ITEM_FEATURES", [])
    for i, feat in enumerate(features[:20]):
        row_dict[f"ITEM_FEATURES_{i+1}"] = feat
        
    dyn_attrs = cpo_data.get("dynamic_attributes", [])
    for i, fact in enumerate(dyn_attrs[:50]):
        idx = i + 1
        row_dict[f"ATTRIBUTE_LABEL {idx}"] = fact.get("attribute", "")
        row_dict[f"ATTRIBUTE_VALUE {idx}"] = fact.get("value", "")
        uom = fact.get("uom", "")
        row_dict[f"ATTRIBUTE_UOM {idx}"] = uom if uom is not None else ""
        
    for res_name, res_url in resources.items():
        if res_name in row_dict:
            row_dict[res_name] = res_url
            
    out_file = os.path.join(output_dir, "final_output.csv")
    with open(out_file, "w", newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(template_headers)
        writer.writerow([row_dict[h] for h in template_headers])
    print(f"[*] Wrote highly-enriched 252-column output to {out_file}")

def main():
    client = QdrantClient(path="qdrant_db_v3")
    try: client.get_collection("evidence")
    except:
        client.create_collection(
            collection_name="evidence",
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
        )
        
    with open('Unihack_ Expected Output - Delivery Format.csv', 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        template_headers = next(reader)
        
    discovery_urls_by_mpn = {}
    if os.path.exists("discovery_debug.json"):
        with open("discovery_debug.json", "r", encoding="utf-8") as f:
            disc_data = json.load(f)
            for item in disc_data:
                mpn = item.get("original_mpn", "")
                urls = []
                for q in item.get("queries", []):
                    for res in q.get("results", []):
                        if res.get("status") == "ACCEPT" or res.get("score", 0) > -30:
                            urls.append(res.get("url"))
                discovery_urls_by_mpn[mpn] = list(dict.fromkeys(urls))
        
    with open("Unihack_ Sample Dataset - Input.csv", 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            if count >= 5: break
            
            raw_mpn = row.get("Mfg_Part_Num", "")
            part_desc = row.get("Part_Desc", "")
            mpn = raw_mpn
            
            output_dir = f"artifacts/{mpn}"
            if not os.path.exists(output_dir):
                count += 1
                continue
                
            print(f"\n{'='*50}\n=== Phase 2 Enrichment: {mpn} ===\n{'='*50}")
            
            # Retrieve more specific facts
            retrieved = retrieve(client, mpn, f"{mpn} {part_desc} technical specifications dimensions material features UPC GTIN brand OPM grade sizes backing")
            
            with open(os.path.join(output_dir, "retrieved_evidence.json"), "w", encoding='utf-8') as f2:
                json.dump(retrieved, f2, indent=2)
                
            cpo_data = extract_facts(mpn, part_desc, retrieved)
            
            with open(os.path.join(output_dir, "cpo.json"), "w", encoding='utf-8') as f2:
                json.dump(cpo_data, f2, indent=2)
                
            disc_urls = discovery_urls_by_mpn.get(mpn, [])
            
            resources = {}
            res_path = os.path.join(output_dir, "resources.json")
            if os.path.exists(res_path):
                with open(res_path, "r", encoding="utf-8") as f3:
                    resources = json.load(f3)
                    
            generate_output(cpo_data, output_dir, row, template_headers, disc_urls, resources)
            count += 1

if __name__ == '__main__':
    main()
