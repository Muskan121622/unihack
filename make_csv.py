import json
import csv
import os

mpn = "3MABR-7100075678"
output_dir = f"artifacts/{mpn}"

with open(f"{output_dir}/cpo.json", "r") as f:
    cpo_data = json.load(f)

with open(f"{output_dir}/resources.json", "r") as f:
    resources = json.load(f)
    
if os.path.exists(f"{output_dir}/retrieved_evidence.json"):
    with open(f"{output_dir}/retrieved_evidence.json", "r", encoding="utf-8") as f4:
        ev = json.load(f4)
        for chunk in ev:
            src = chunk.get("source_url", "")
            src_lower = src.lower()
            if ".pdf" in src_lower:
                if "sds" in src_lower or "msds" in src_lower or "safety" in src_lower or "upd56" in src_lower:
                    resources["SDS"] = src
                elif "datasheet" in src_lower or "technical" in src_lower or "spec" in src_lower:
                    resources["Specification Sheet"] = src
                    
print("RESOURCES WITH PDF INJECT:", json.dumps(resources, indent=2))

with open("discovery_debug.json", "r", encoding="utf-8") as f:
    disc_data = json.load(f)
    discovery_urls = []
    for item in disc_data:
        if item.get("original_mpn") == mpn:
            for q in item.get("queries", []):
                for res in q.get("results", []):
                    if res.get("status") == "ACCEPT" or res.get("score", 0) > -30:
                        discovery_urls.append(res.get("url"))
            break
    discovery_urls = list(dict.fromkeys(discovery_urls))

with open("Unihack_ Expected Output - Delivery Format.csv", "r", encoding="utf-8") as f:
    template_headers = next(csv.reader(f))

with open("Unihack_ Sample Dataset - Input.csv", "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row["Mfg_Part_Num"] == mpn:
            input_row = row
            break
            
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
dyn_attrs.sort(key=lambda x: x.get("attribute", ""))

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
with open(out_file, "w", newline='', encoding='utf-8') as f_out:
    writer = csv.writer(f_out)
    writer.writerow(template_headers)
    writer.writerow([row_dict[h] for h in template_headers])
    
print("Successfully generated final_output.csv!")
