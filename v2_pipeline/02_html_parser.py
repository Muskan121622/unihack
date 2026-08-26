"""
Stage 2: HTML Parser
====================
For each ref_url (real product page), fetch and extract structured evidence.
No chunking. No embedding. Pure deterministic structured extraction.

Extracts:
  - Product name (og:title, h1, .product-title)
  - Spec tables (th/td, dl/dt/dd, JSON-LD)
  - UPC / EAN / GTIN (regex near barcode labels)
  - Images with MPN in path
  - PDF document links (with label text for PDF type classification)
  - Description paragraphs (candidate for embedding — kept separate)

Output per page: dict saved to artifacts/{MPN}/html_evidence_{n}.json
"""

import re
import json
import time
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Regex for UPC/EAN/GTIN detection (10–14 digit sequences)
UPC_PATTERN = re.compile(r"\b(\d{12,14})\b")

# Labels that commonly precede a barcode value on product pages
BARCODE_LABELS = ["upc", "ean", "gtin", "barcode", "item #", "upc-a", "ean-13"]


def _fetch_html(url: str, timeout: int = 10) -> BeautifulSoup | None:
    """Fetch page and return BeautifulSoup object, or None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return BeautifulSoup(resp.content, "html.parser")
        else:
            print(f"    [!] HTTP {resp.status_code} for {url}")
            return None
    except Exception as e:
        print(f"    [!] Fetch error for {url}: {e}")
        return None


def _extract_json_ld(soup: BeautifulSoup) -> dict:
    """Extract structured data from JSON-LD blocks."""
    facts = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict):
                # Product schema
                t = data.get("@type", "")
                if t in ("Product", "IndividualProduct"):
                    if data.get("name"):
                        facts["product_name"] = data["name"]
                    if data.get("description"):
                        facts["json_ld_description"] = data["description"]
                    if data.get("sku"):
                        facts["sku"] = data["sku"]
                    if data.get("mpn"):
                        facts["json_ld_mpn"] = data["mpn"]
                    if data.get("brand"):
                        brand = data["brand"]
                        if isinstance(brand, dict):
                            facts["brand"] = brand.get("name", "")
                        else:
                            facts["brand"] = str(brand)
                    # Offers
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0]
                    if isinstance(offers, dict):
                        if offers.get("price"):
                            facts["list_price"] = str(offers["price"])
                        if offers.get("priceCurrency"):
                            facts["price_currency"] = offers["priceCurrency"]
                    # Identifiers
                    for id_field in ["gtin13", "gtin12", "gtin14", "gtin", "isbn"]:
                        if data.get(id_field):
                            facts["gtin"] = str(data[id_field])
                            break
                    # Aggregate rating
                    agg = data.get("aggregateRating", {})
                    if agg:
                        facts["rating"] = agg.get("ratingValue", "")
                        facts["review_count"] = agg.get("reviewCount", "")
        except Exception:
            continue
    return facts


def _extract_spec_tables(soup: BeautifulSoup) -> dict:
    """Extract key:value pairs from spec tables (th/td, dl/dt/dd)."""
    specs = {}

    # Standard tables
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key and val and len(key) < 80 and len(val) < 500:
                    specs[key] = val

    # Definition lists (dl/dt/dd)
    for dl in soup.find_all("dl"):
        terms = dl.find_all("dt")
        defs = dl.find_all("dd")
        for dt, dd in zip(terms, defs):
            key = dt.get_text(strip=True)
            val = dd.get_text(strip=True)
            if key and val and len(key) < 80 and len(val) < 500:
                specs[key] = val

    # Div-based spec rows (common in React/Shopify stores)
    # Pattern: .spec-row / .specification / [data-spec]
    for container in soup.find_all(
        attrs={"class": re.compile(r"spec|attribute|detail|feature", re.I)}
    ):
        children = container.find_all(["span", "div", "p"], limit=2)
        if len(children) >= 2:
            key = children[0].get_text(strip=True)
            val = children[1].get_text(strip=True)
            if key and val and len(key) < 80 and len(val) < 500 and key != val:
                specs[key] = val

    return specs


def _extract_barcodes(soup: BeautifulSoup, full_text: str) -> dict:
    """Extract UPC/EAN/GTIN values using regex anchored near label words."""
    barcodes = {}
    text_lower = full_text.lower()

    for label in BARCODE_LABELS:
        idx = text_lower.find(label)
        while idx != -1:
            # Look for a number within the next 80 characters
            snippet = full_text[idx: idx + 80]
            m = UPC_PATTERN.search(snippet)
            if m:
                val = m.group(1)
                if label in ["upc", "upc-a"] and len(val) == 12:
                    barcodes["UPC"] = val
                elif label in ["ean", "ean-13"] and len(val) == 13:
                    barcodes["EAN"] = val
                elif label == "gtin":
                    barcodes["GTIN"] = val
                else:
                    # Generic — use length to decide
                    if len(val) == 12:
                        barcodes.setdefault("UPC", val)
                    elif len(val) == 13:
                        barcodes.setdefault("EAN", val)
                    elif len(val) == 14:
                        barcodes.setdefault("GTIN", val)
            idx = text_lower.find(label, idx + 1)

    return barcodes


def _extract_images(soup: BeautifulSoup, base_url: str, mpn: str) -> list:
    """Extract product images. Prefer those with MPN in the URL path."""
    images = []
    seen = set()

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        alt = img.get("alt", "")
        if not src:
            continue
        full_url = urljoin(base_url, src)
        if full_url in seen:
            continue
        path = urlparse(full_url).path.lower()
        # Must have an image extension
        if not any(path.endswith(ext) for ext in IMAGE_EXTENSIONS):
            continue
        # Filter junk
        bad_terms = ["logo", "icon", "favicon", "payment", "social", "badge",
                     "star", "arrow", "sprite", "pixel", "spacer", "1x1", "2x2"]
        combined = (full_url + " " + alt).lower()
        if any(t in combined for t in bad_terms):
            continue
        # Score: MPN in URL = high priority
        score = 10 if mpn.lower() in full_url.lower() else 0
        score += 5 if "product" in full_url.lower() else 0
        images.append({"url": full_url, "alt": alt, "score": score})
        seen.add(full_url)

    images.sort(key=lambda x: x["score"], reverse=True)
    return [img["url"] for img in images[:8]]  # max 8 per page


def _extract_pdf_links(soup: BeautifulSoup, base_url: str) -> list:
    """Extract all links to PDF documents with their anchor text."""
    pdf_links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if ".pdf" in full_url.lower() or "/pdf/" in full_url.lower():
            label = a.get_text(strip=True) or ""
            pdf_links.append({"url": full_url, "label": label})
            seen.add(full_url)
    return pdf_links


def _extract_description_paragraphs(soup: BeautifulSoup, mpn: str) -> list:
    """
    Extract unstructured description text that may need embedding.
    Only paragraphs/lists that contain real content (>50 chars).
    Excludes navigation, footer, legal boilerplate.
    """
    candidates = []
    skip_parents = {"nav", "footer", "header", "aside", "script", "style"}

    for elem in soup.find_all(["p", "li", "div"]):
        # Skip if inside bad containers
        if any(p.name in skip_parents for p in elem.parents):
            continue
        text = elem.get_text(separator=" ", strip=True)
        if len(text) < 60:
            continue
        if len(text) > 2000:
            continue
        # Must mention something product-related or MPN
        text_lower = text.lower()
        is_relevant = (
            mpn.lower() in text_lower
            or any(kw in text_lower for kw in [
                "feature", "application", "specification", "material",
                "dimension", "weight", "grit", "voltage", "amperage",
                "warranty", "includes", "compatible", "designed for"
            ])
        )
        if is_relevant:
            candidates.append(text)

    # De-duplicate (many pages repeat text in different containers)
    seen_texts = set()
    unique = []
    for t in candidates:
        key = t[:100]
        if key not in seen_texts:
            seen_texts.add(key)
            unique.append(t)

    return unique[:10]  # cap at 10 paragraphs per page


def parse_product_page(url: str, mpn: str, brand: str) -> dict | None:
    """
    Parse a single product page and return structured evidence.

    Returns None if the MPN is not found on the page (not a valid product page).
    """
    print(f"    [HTML] Parsing: {url[:80]}...")
    soup = _fetch_html(url)
    if not soup:
        return None

    full_text = soup.get_text(separator=" ", strip=True)

    # Verify MPN is actually on this page (before spending effort)
    # Use normalized comparison (strip hyphens/spaces)
    mpn_norm = re.sub(r"[\s\-]", "", mpn).lower()
    text_norm = re.sub(r"[\s\-]", "", full_text).lower()
    if mpn_norm not in text_norm:
        print(f"    [!] MPN '{mpn}' not found on page — skipping")
        return None

    # Product name
    product_name = ""
    # Try og:title first
    og_title = soup.find("meta", property="og:title")
    if og_title:
        product_name = og_title.get("content", "")
    # Fall back to h1
    if not product_name:
        h1 = soup.find("h1")
        if h1:
            product_name = h1.get_text(strip=True)
    # Fall back to page title
    if not product_name and soup.title:
        product_name = soup.title.string or ""

    # Structured extraction
    json_ld_facts = _extract_json_ld(soup)
    specs = _extract_spec_tables(soup)
    barcodes = _extract_barcodes(soup, full_text)
    images = _extract_images(soup, url, mpn)
    pdf_links = _extract_pdf_links(soup, url)
    description_paragraphs = _extract_description_paragraphs(soup, mpn)

    return {
        "source_url": url,
        "product_name": product_name,
        "json_ld": json_ld_facts,
        "specs": specs,
        "barcodes": barcodes,
        "images": images,
        "pdf_links": pdf_links,
        "description_paragraphs": description_paragraphs,
        "full_text_length": len(full_text),
    }


def parse_all_pages(ref_urls: list, mpn: str, brand: str,
                    mfr_url: str = None, max_pages: int = 5) -> list:
    """
    Parse up to max_pages product pages for a given MPN.

    Includes mfr_url at the front if provided.
    Returns list of evidence dicts.
    """
    urls_to_parse = []
    if mfr_url:
        urls_to_parse.append(mfr_url)
    for u in ref_urls:
        if u != mfr_url:
            urls_to_parse.append(u)
    urls_to_parse = urls_to_parse[:max_pages]

    results = []
    for i, url in enumerate(urls_to_parse):
        evidence = parse_product_page(url, mpn, brand)
        if evidence:
            results.append(evidence)
        time.sleep(0.5)  # polite crawl delay

    print(f"    [HTML] Parsed {len(results)}/{len(urls_to_parse)} pages successfully")
    return results


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    # Quick test on first product's first accepted URL
    test_url = "https://www.acehardware.com/departments/tools/power-tool-accessories/sanding-belts/1035181"
    result = parse_product_page(test_url, "DCB518ASTS06G", "Diablo")
    if result:
        print(json.dumps({
            "product_name": result["product_name"],
            "specs_count": len(result["specs"]),
            "barcodes": result["barcodes"],
            "images_count": len(result["images"]),
            "pdf_links": result["pdf_links"],
            "description_paragraphs_count": len(result["description_paragraphs"]),
        }, indent=2))
