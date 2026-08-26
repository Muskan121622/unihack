import os
import json
import csv
import requests
import time
import uuid
import datetime
import traceback

# Handle multiple keys if provided (comma-separated) for fallback/rotation
TAVILY_API_KEYS = [k.strip() for k in os.getenv("TAVILY_API_KEY", "").split(",") if k.strip()]
_current_key_idx = 0

BATCH_SIZE = 50
MAX_RETRIES = 5

def get_tavily_key():
    if not TAVILY_API_KEYS:
        return None
    return TAVILY_API_KEYS[_current_key_idx]

def rotate_tavily_key():
    global _current_key_idx
    if len(TAVILY_API_KEYS) > 1:
        _current_key_idx = (_current_key_idx + 1) % len(TAVILY_API_KEYS)
        print(f"[*] Rotated Tavily API Key to index {_current_key_idx}")

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
    url = "https://api.tavily.com/search"
    
    for attempt in range(1, MAX_RETRIES + 1):
        api_key = get_tavily_key()
        if not api_key:
            print("[-] TAVILY_API_KEY not set!")
            return []
            
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "include_raw_content": False,
            "max_results": 5
        }
        
        try:
            response = requests.post(url, json=payload, timeout=15)
            
            if response.status_code == 200:
                return response.json().get("results", [])
            elif response.status_code in [429, 401, 403, 432]:
                print(f"[-] Tavily API error {response.status_code}: {response.text}")
                rotate_tavily_key()
                # Exponential backoff
                wait_time = min(60, (2 ** attempt) + 2)
                print(f"[*] Backing off for {wait_time}s before retry...")
                time.sleep(wait_time)
            elif response.status_code >= 500:
                print(f"[-] Tavily Server error {response.status_code}: {response.text}")
                time.sleep(5)
            else:
                print(f"[-] Unhandled Tavily error {response.status_code}: {response.text}")
                break
                
        except requests.exceptions.RequestException as e:
            print(f"[-] Network error during Tavily search: {e}")
            time.sleep(5)
            
    print(f"[-] Failed to fetch results for query '{query}' after {MAX_RETRIES} attempts.")
    return []

def verify_url(result, mpn, brand):
    url = result.get("url", "").lower()
    title = result.get("title", "").upper()
    content = result.get("content", "").upper()
    
    score = 0
    reasons = []
    
    mpn_upper = mpn.upper()
    brand_upper = brand.upper()
    
    if mpn_upper in content:
        score += 50
        reasons.append("+50 Exact MPN in content")
    else:
        score -= 60
        reasons.append("-60 MPN absent from content")
        
    if mpn_upper in title:
        score += 20
        reasons.append("+20 Exact MPN in title")
        
    if brand_upper in content or brand_upper in title:
        score += 15
        reasons.append("+15 Brand in content/title")
        
    official_domains = ["diablotools.com", "freudtools.com", "3m.com", "multimedia.3m.com"]
    bad_domains = ["youtube.com", "facebook.com", "twitter.com", "instagram.com", "reddit.com", "sbs.com.au", "microsoft.com", "google.com", "walmart.com", "amazon.com", "ebay.com"]
    
    if any(domain in url for domain in official_domains):
        score += 30
        reasons.append("+30 Official manufacturer domain")
    
    if any(domain in url for domain in bad_domains):
        score -= 100
        reasons.append("-100 Bad/Social/Marketplace domain")
        
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

def atomic_write_json(data, filepath):
    tmp_path = f"{filepath}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception as e:
        print(f"[-] Atomic write failed for {filepath}: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def build_unique_key(brand, mpn):
    return f"{brand.lower().strip()}|{mpn.lower().strip()}"

def run_discovery_pipeline(limit=None):
    input_csv = "Unihack_ Sample Dataset - Input.csv"
    debug_json = "discovery_debug.json"
    state_json = "discovery_state.json"
    
    print("="*60)
    print(" STAGE 0: TAVILY DISCOVERY PIPELINE")
    print("="*60)

    # 1. Load input and deduplicate
    all_products = []
    seen_keys = set()
    
    with open(input_csv, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_mpn = row.get("Mfg_Part_Num", "").strip()
            if not raw_mpn:
                continue
            part_desc = row.get("Part_Desc", "")
            manufacturer = row.get("Part_Manuf", "")
            brand, mpn = extract_brand_and_mpn(raw_mpn, part_desc, manufacturer)
            
            key = build_unique_key(brand, mpn)
            if key not in seen_keys:
                seen_keys.add(key)
                all_products.append({
                    "raw_mpn": raw_mpn,
                    "mpn": mpn,
                    "brand": brand,
                    "desc": part_desc,
                    "key": key
                })
                
    total_unique = len(all_products)
    print(f"[*] Parsed CSV. Found {total_unique} unique products.")

    # 2. Load existing results for crash recovery
    completed_keys = set()
    debug_report = []
    
    if os.path.exists(debug_json):
        try:
            with open(debug_json, "r", encoding="utf-8") as f:
                debug_report = json.load(f)
                for item in debug_report:
                    c_key = build_unique_key(item.get("resolved_brand", ""), item.get("mpn", ""))
                    completed_keys.add(c_key)
            print(f"[*] Loaded existing {debug_json}. {len(completed_keys)} products already completed.")
        except Exception as e:
            print(f"[-] Error loading {debug_json}: {e}. Proceeding fresh.")
            # If it's corrupted, we back it up
            if os.path.getsize(debug_json) > 0:
                os.rename(debug_json, f"{debug_json}.corrupt.bak")

    # 3. Filter pending
    pending_products = [p for p in all_products if p["key"] not in completed_keys]
    
    if limit:
        pending_products = pending_products[:limit]
        print(f"[*] Limit applied. Processing {len(pending_products)} products.")
    else:
        print(f"[*] Processing {len(pending_products)} pending products.")

    state = {
        "total_unique": total_unique,
        "completed_count": len(completed_keys),
        "pending_count": len(pending_products),
        "started_at": datetime.datetime.now().isoformat(),
        "status": "running"
    }

    # 4. Process in batches
    start_time = time.time()
    tavily_calls = 0
    successful_searches = 0
    
    for batch_idx in range(0, len(pending_products), BATCH_SIZE):
        batch = pending_products[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = (batch_idx // BATCH_SIZE) + 1
        total_batches = (len(pending_products) + BATCH_SIZE - 1) // BATCH_SIZE
        
        print(f"\n{'-'*60}\n STARTING BATCH {batch_num}/{total_batches} ({len(batch)} items)\n{'-'*60}")
        
        for i, p in enumerate(batch):
            row_num = batch_idx + i + 1
            mpn = p["mpn"]
            brand = p["brand"]
            print(f"\n[Row {row_num}/{len(pending_products)}] {brand} | {mpn}")
            
            queries = [
                f'"{mpn}"',
                f'"{mpn}" {brand}',
                f'"{mpn}" specifications OR datasheet',
                f'"{mpn}" PDF',
                f'site:3m.com "{mpn}"' if brand == "3M" else f'site:diablotools.com "{mpn}"'
            ]
            
            product_report = {
                "original_mpn": p["raw_mpn"],
                "mpn": mpn,
                "resolved_brand": brand,
                "description": p["desc"],
                "queries": []
            }
            
            all_verifications = {}
            row_success = True
            
            try:
                for query in queries:
                    print(f"    -> Query: {query}")
                    tavily_calls += 1
                    results = search_tavily(query)
                    
                    if results:
                        successful_searches += 1
                    
                    query_log = {"query": query, "results": []}
                    for r in results:
                        url = r.get("url")
                        if url not in all_verifications:
                            verification = verify_url(r, mpn, brand)
                            all_verifications[url] = verification
                        else:
                            verification = all_verifications[url]
                        query_log["results"].append(verification)
                    
                    product_report["queries"].append(query_log)
                    time.sleep(1) # Base rate limit padding
                    
                # Compile results
                sorted_verifications = sorted(all_verifications.values(), key=lambda x: x["score"], reverse=True)
                accepted = [v for v in sorted_verifications if v["status"] == "ACCEPT"]
                
                product_report["accepted"] = accepted[:3]
                product_report["review"] = [v for v in sorted_verifications if v["status"] == "REVIEW"]
                product_report["rejected"] = [v for v in sorted_verifications if v["status"] == "REJECT"]
                product_report["discovery_status"] = "SUCCESS" if len(accepted) > 0 else "NO_EVIDENCE"
                
                print(f"    -> STATUS: {product_report['discovery_status']} ({len(accepted[:3])} accepted URLs)")
            
            except Exception as e:
                print(f"    [-] CRITICAL ERROR ON ROW: {e}")
                traceback.print_exc()
                product_report["discovery_status"] = "FAILED"
                product_report["error"] = str(e)
            
            # 5. Incremental Persist / Checkpoint
            debug_report.append(product_report)
            atomic_write_json(debug_report, debug_json)
            
            state["completed_count"] += 1
            state["last_mpn"] = mpn
            state["updated_at"] = datetime.datetime.now().isoformat()
            state["elapsed_seconds"] = int(time.time() - start_time)
            
            # Calculate estimates
            processed_in_this_run = state["completed_count"] - len(completed_keys)
            if processed_in_this_run > 0:
                avg_time_per_row = state["elapsed_seconds"] / processed_in_this_run
                remaining = len(pending_products) - processed_in_this_run
                state["estimated_remaining_seconds"] = int(avg_time_per_row * remaining)
                print(f"    [!] Avg {avg_time_per_row:.1f}s/row. ETA: {state['estimated_remaining_seconds']//60}m {state['estimated_remaining_seconds']%60}s")
                
            atomic_write_json(state, state_json)
            
    print(f"\n{'='*60}\n PIPELINE COMPLETED \n{'='*60}")
    print(f"Processed {len(pending_products)} rows in {int(time.time() - start_time)} seconds.")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--benchmark', type=int, default=None, help="Process only N rows for benchmarking")
    args = parser.parse_args()
    
    try:
        run_discovery_pipeline(limit=args.benchmark)
    except KeyboardInterrupt:
        print("\n[!] Gracefully interrupted by user. State is saved.")
        # Ensure state reflects interrupt
        if os.path.exists("discovery_state.json"):
            with open("discovery_state.json", "r", encoding="utf-8") as f:
                state = json.load(f)
            state["status"] = "interrupted"
            atomic_write_json(state, "discovery_state.json")
