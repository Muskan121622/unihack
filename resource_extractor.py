import os
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

def classify_url(url, text_label="", title="", mpn=""):
    url_lower = url.lower()
    text_lower = text_label.lower()
    title_lower = title.lower()
    
    context = url_lower + " " + text_lower + " " + title_lower
    
    if any(ext in url_lower for ext in [".jpg", ".png", ".webp", ".jpeg", ".gif", ".bmp"]):
        if any(bad in context for bad in ["logo", "icon", "payment", "facebook", "twitter", "instagram", "youtube", "social"]):
            return None
        return "Image"
        
    is_pdf = ".pdf" in url_lower or "download=true" in url_lower
    is_doc = is_pdf or "/doc/" in url_lower or "/document/" in url_lower or "/manuals/" in url_lower
    
    scores = {
        "SDS": 0,
        "Specification Sheet": 0,
        "Catalog": 0,
        "Instruction/Installation Manual": 0,
        "Warranty Information": 0,
        "Video Link": 0,
        "RoHS": 0,
        "Size Chart": 0
    }
    
    if "youtube.com" in url_lower or "youtu.be" in url_lower or (("video" in text_lower) and not is_doc):
        scores["Video Link"] += 10
        
    if is_doc or "sds" in text_lower or "msds" in text_lower:
        if "sds" in text_lower or "safety data sheet" in text_lower or "msds" in text_lower or "-sds" in url_lower or "sds" in url_lower:
            scores["SDS"] += 10
        if "spec" in text_lower or "tds" in text_lower or "technical data" in text_lower or "datasheet" in text_lower or "data sheet" in text_lower:
            scores["Specification Sheet"] += 10
        if "catalog" in text_lower or "brochure" in text_lower:
            scores["Catalog"] += 10
        if "manual" in text_lower or "instruction" in text_lower or "user guide" in text_lower:
            scores["Instruction/Installation Manual"] += 10
        if "warranty" in text_lower:
            scores["Warranty Information"] += 10
            
    if not is_doc:
        if "/c-" in url_lower or "category" in url_lower or "catalogsearch" in url_lower or "search" in url_lower:
            return None
            
    best_cat = max(scores, key=scores.get)
    if scores[best_cat] >= 10:
        return best_cat
        
    return None

def extract_resources(mpn, urls):
    resources = {k: [] for k in ["Image", "SDS", "Specification Sheet", "Catalog", "Instruction/Installation Manual", "Warranty Information", "Video Link", "RoHS", "Size Chart"]}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    # Classify the base URLs first!
    for url in urls:
        cls = classify_url(url, "", "", mpn)
        if cls and url not in resources[cls]:
            resources[cls].append(url)
            
    for url in urls[:15]:
        if ".pdf" in url.lower(): continue # Don't parse PDFs as HTML
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code != 200: continue
            soup = BeautifulSoup(resp.content, "html.parser")
            page_title = soup.title.string if soup.title else ""
            
            for img in soup.find_all("img"):
                src = img.get("src")
                if src:
                    full_url = urljoin(url, src)
                    alt = img.get("alt", "")
                    cls = classify_url(full_url, alt, page_title, mpn)
                    if cls == "Image" and full_url not in resources["Image"]:
                        resources["Image"].append(full_url)
                        
            for a in soup.find_all("a"):
                href = a.get("href")
                if href and not href.startswith("#") and "javascript:" not in href:
                    full_url = urljoin(url, href)
                    text = a.get_text(strip=True)
                    cls = classify_url(full_url, text, page_title, mpn)
                    if cls and cls != "Image" and full_url not in resources[cls]:
                        resources[cls].append(full_url)
        except:
            pass
            
    final_resources = {}
    
    images = resources["Image"]
    def img_score(img_url):
        score = 0
        if mpn.lower() in img_url.lower(): score += 10
        if "product" in img_url.lower(): score += 2
        return score
    images.sort(key=img_score, reverse=True)
    
    if len(images) > 0: final_resources["Product Image"] = images[0]
    if len(images) > 1: final_resources["Alternate Image 1"] = images[1]
    if len(images) > 2: final_resources["Alternate Image 2"] = images[2]
    if len(images) > 3: final_resources["Alternate Image 3"] = images[3]
    if len(images) > 4: final_resources["Alternate Image 4"] = images[4]
    
    for k in ["SDS", "Specification Sheet", "Catalog", "Instruction/Installation Manual", "Warranty Information", "Video Link", "RoHS", "Size Chart"]:
        if resources[k]:
            docs = resources[k]
            docs.sort(key=lambda x: 1 if mpn.lower() in x.lower() else 0, reverse=True)
            final_resources[k] = docs[0]
            
    return final_resources

if __name__ == '__main__':
    with open("discovery_debug.json", "r", encoding="utf-8") as f:
        disc_data = json.load(f)
    for item in disc_data:
        mpn = item.get("original_mpn", "")
        if mpn != "3MABR-7100075678": continue
        urls = []
        for q in item.get("queries", []):
            for res in q.get("results", []):
                if res.get("status") == "ACCEPT" or res.get("score", 0) > -30:
                    urls.append(res.get("url"))
        urls = list(dict.fromkeys(urls))
        res = extract_resources(mpn, urls)
        print(json.dumps(res, indent=2))
        with open(f"artifacts/{mpn}/resources.json", "w", encoding="utf-8") as f2:
            json.dump(res, f2, indent=2)
