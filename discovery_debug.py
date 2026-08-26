import os
import json
import csv
import requests
import time

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

def extract_brand_and_mpn(mpn, part_desc, part_manuf):
    desc_upper = part_desc.upper()
    manuf_upper = part_manuf.upper()
    
    brand = part_manuf.split("(")[0].strip()
    if "3M" in desc_upper or "3M" in manuf_upper or part_desc.startswith("3M"):
        brand = "3M"
    elif "DIABLO" in desc_upper or "FREUD" in manuf_upper:
        brand = "Diablo"
        
    real_mpn = mpn
    if brand == "3M" and mpn.startswith("3MABR-"):
        real_mpn = mpn.replace("3MABR-", "")
        
    return brand, real_mpn

def search_tavily(query):
    if not TAVILY_API_KEY:
        print("[-] TAVILY_API_KEY not set! Please set it via $env:TAVILY_API_KEY")
        return []
        
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "include_raw_content": False,
        "max_results": 5
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return response.json().get("results", [])
        else:
            print(f"[-] Tavily API error: {response.status_code} {response.text}")
    except Exception as e:
        print(f"[-] Tavily Request failed: {e}")
    return []

def verify_url(result, mpn, brand):
    url = result.get("url", "").lower()
    title = result.get("title", "").upper()
    content = result.get("content", "").upper()
    
    score = 0
    reasons = []
    
    mpn_upper = mpn.upper()
    brand_upper = brand.upper()
    
    # Check MPN
    if mpn_upper in content:
        score += 50
        reasons.append("+50 Exact MPN in content")
    else:
        score -= 60
        reasons.append("-60 MPN absent from content")
        
    if mpn_upper in title:
        score += 20
        reasons.append("+20 Exact MPN in title")
        
    # Check Brand
    if brand_upper in content or brand_upper in title:
        score += 15
        reasons.append("+15 Brand in content/title")
        
    # Domain checks
    official_domains = ["diablotools.com", "freudtools.com", "3m.com", "multimedia.3m.com"]
    bad_domains = ["youtube.com", "facebook.com", "twitter.com", "instagram.com", "reddit.com", "sbs.com.au", "microsoft.com", "google.com", "walmart.com", "amazon.com", "ebay.com"]
    
    if any(domain in url for domain in official_domains):
        score += 30
        reasons.append("+30 Official manufacturer domain")
    
    if any(domain in url for domain in bad_domains):
        score -= 100
        reasons.append("-100 Bad/Social/Marketplace domain")
        
    # Document type checks
    if ".pdf" in url or "pdf" in title:
        score += 15
        reasons.append("+15 PDF document")
        
    if any(kw in title or kw in url for kw in ["tds", "specification", "datasheet", "catalog", "spec"]):
        score += 15
        reasons.append("+15 TDS/spec/catalog")
        
    status = "ACCEPT" if score >= 70 else ("REVIEW" if score >= 40 else "REJECT")
    
    return {
        "url": result.get("url"),
        "title": result.get("title"),
        "score": score,
        "status": status,
        "reasons": reasons
    }

def debug_discovery():
    input_csv = "Unihack_ Sample Dataset - Input.csv"
    count = 0
    
    debug_report = []
    
    with open(input_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if count >= 5:
                break
                
            raw_mpn = row.get("Mfg_Part_Num", "")
            part_desc = row.get("Part_Desc", "")
            manufacturer = row.get("Part_Manuf", "")
            brand, mpn = extract_brand_and_mpn(raw_mpn, part_desc, manufacturer)
            
            print(f"\n{'='*50}\nPRODUCT: {mpn} (Original: {raw_mpn})\n{'='*50}")
            print(f"DESCRIPTION: {part_desc}")
            print(f"RESOLVED BRAND: {brand}\n")
            
            queries = [
                f'"{mpn}"',
                f'"{mpn}" {brand}',
                f'"{mpn}" specifications OR datasheet',
                f'"{mpn}" PDF',
                f'site:3m.com "{mpn}"' if brand == "3M" else f'site:diablotools.com "{mpn}"'
            ]
            
            product_report = {
                "original_mpn": raw_mpn,
                "mpn": mpn,
                "resolved_brand": brand,
                "description": part_desc,
                "queries": []
            }
            
            all_verifications = {}
            
            for query in queries:
                print(f"--- QUERY: {query} ---")
                results = search_tavily(query)
                
                query_log = {
                    "query": query,
                    "results": []
                }
                
                for r in results:
                    url = r.get("url")
                    if url not in all_verifications:
                        verification = verify_url(r, mpn, brand)
                        all_verifications[url] = verification
                    else:
                        verification = all_verifications[url]
                        
                    query_log["results"].append(verification)
                
                product_report["queries"].append(query_log)
                time.sleep(1) # Be nice to Tavily API
                
            # Sort all unique verified URLs by score
            sorted_verifications = sorted(all_verifications.values(), key=lambda x: x["score"], reverse=True)
            
            accepted_urls = [v for v in sorted_verifications if v["status"] == "ACCEPT"]
            review_urls = [v for v in sorted_verifications if v["status"] == "REVIEW"]
            rejected_urls = [v for v in sorted_verifications if v["status"] == "REJECT"]
            
            # Take top 3 accepted
            top_accepted = accepted_urls[:3]
            
            product_report["accepted"] = top_accepted
            product_report["review"] = review_urls
            product_report["rejected"] = rejected_urls
            product_report["discovery_status"] = "SUCCESS" if len(top_accepted) > 0 else "FAILED"
            
            print(f"\nDISCOVERY STATUS: {product_report['discovery_status']}")
            for acc in top_accepted:
                print(f"[ACCEPT] Score {acc['score']}: {acc['url']}")
            
            debug_report.append(product_report)
            count += 1
            
    with open("discovery_debug.json", "w", encoding='utf-8') as f:
        json.dump(debug_report, f, indent=2)
        
    print("\nWrote discovery_debug.json")

if __name__ == '__main__':
    debug_discovery()
