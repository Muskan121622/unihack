"""
Stage 2 — HTML Parser
======================
Fetches each accepted product page and extracts structured evidence.
No chunking. No embedding. Pure deterministic extraction.

Goal: fill as many of the 252 CSV columns as possible directly —
  specs, barcodes, images, PDF links — before anything goes to Qdrant.

Target columns (direct from HTML):
  - Product Name, BRAND_NAME, MANUFACTURER_NAME, TRADE_NAME
  - UPC / EAN / GTIN (regex scan + JSON-LD)
  - LENGTH, HEIGHT, WIDTH, WEIGHT (from spec tables)
  - ITEM_FEATURES_* (bullet lists)
  - ATTRIBUTE_LABEL/VALUE/UOM (spec table rows)
  - Product Image, Alternate Image 1–4
  - SDS, Catalog, Specification Sheet, Manual links
  - MARKETING_DESCRIPTION, SHORT_DESC, LONG_DESC1
  - WARRANTY, Country Of Origin

Interface contract with Stage 4:
  Returns list of dicts, each with keys:
    source_url, product_name, json_ld, specs, barcodes,
    images, pdf_links, description_paragraphs, features, full_text_length
"""

from __future__ import annotations
import re
import json
import time
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Tag

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 12
CRAWL_DELAY = 0.6   # seconds between requests

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_IMAGE_BAD = {"logo", "icon", "favicon", "payment", "social", "badge",
               "star", "arrow", "sprite", "pixel", "spacer", "1x1", "2x2", "blank"}

# UPC/EAN/GTIN: 8–14 consecutive digits
_BARCODE_RE = re.compile(r"\b(\d{8,14})\b")
_BARCODE_LABELS = {
    "upc": "UPC", "upc-a": "UPC",
    "ean": "EAN", "ean-13": "EAN",
    "gtin": "GTIN", "gtin-13": "GTIN", "gtin-12": "GTIN",
    "barcode": "UPC",
}

# Dimension extraction from spec values
_DIM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(in(?:ch(?:es)?)?|ft|mm|cm|m|oz|lb(?:s)?|g|kg|fl\.?\s*oz|gal|l)\b",
    re.IGNORECASE,
)


def _fetch(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            return BeautifulSoup(r.content, "html.parser")
        print(f"    [HTML] HTTP {r.status_code}: {url[:70]}")
    except Exception as e:
        print(f"    [HTML] Error: {e} — {url[:70]}")
    return None


def _json_ld(soup: BeautifulSoup) -> dict:
    """Extract Product JSON-LD fields."""
    facts: dict = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(tag.string or "[]")
            items = raw if isinstance(raw, list) else [raw]
            for obj in items:
                if not isinstance(obj, dict):
                    continue
                t = obj.get("@type", "")
                if t not in ("Product", "IndividualProduct"):
                    continue
                facts.setdefault("product_name", obj.get("name"))
                facts.setdefault("description", obj.get("description"))
                facts.setdefault("sku", obj.get("sku"))
                facts.setdefault("mpn_ld", obj.get("mpn"))
                brand = obj.get("brand", {})
                facts.setdefault("brand", brand.get("name") if isinstance(brand, dict) else str(brand))
                mfr = obj.get("manufacturer", {})
                facts.setdefault("manufacturer", mfr.get("name") if isinstance(mfr, dict) else str(mfr))
                for gf in ("gtin14", "gtin13", "gtin12", "gtin", "isbn"):
                    if obj.get(gf):
                        facts.setdefault("gtin", str(obj[gf])); break
                offers = obj.get("offers", {})
                if isinstance(offers, list): offers = offers[0]
                if isinstance(offers, dict):
                    facts.setdefault("list_price", str(offers.get("price", "")))
                    facts.setdefault("price_currency", offers.get("priceCurrency", ""))
                # Warranty from additionalProperty
                for prop in obj.get("additionalProperty", []):
                    if isinstance(prop, dict):
                        pname = prop.get("name", "").lower()
                        if "warrant" in pname:
                            facts.setdefault("warranty", prop.get("value", ""))
                        if "country" in pname or "origin" in pname:
                            facts.setdefault("country_of_origin", prop.get("value", ""))
        except Exception:
            pass
    return {k: v for k, v in facts.items() if v}


def _spec_tables(soup: BeautifulSoup) -> dict:
    """Extract key:value pairs from every table and definition list."""
    specs: dict = {}

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                k = cells[0].get_text(" ", strip=True)
                v = cells[1].get_text(" ", strip=True)
                if k and v and len(k) < 100 and len(v) < 600:
                    specs.setdefault(k, v)

    for dl in soup.find_all("dl"):
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            k = dt.get_text(" ", strip=True)
            v = dd.get_text(" ", strip=True)
            if k and v and len(k) < 100 and len(v) < 600:
                specs.setdefault(k, v)

    # React/Shopify style: divs with class matching "spec|attribute|detail"
    for cont in soup.find_all(attrs={"class": re.compile(r"spec|attribute|detail|feature", re.I)}):
        children = [c for c in cont.children if isinstance(c, Tag)]
        if len(children) >= 2:
            k = children[0].get_text(" ", strip=True)
            v = children[1].get_text(" ", strip=True)
            if k and v and len(k) < 100 and len(v) < 600 and k != v:
                specs.setdefault(k, v)

    return specs


def _barcodes(soup: BeautifulSoup, full_text: str) -> dict:
    """Find UPC/EAN/GTIN by scanning text near label words."""
    found: dict = {}
    text_lower = full_text.lower()
    for label, col in _BARCODE_LABELS.items():
        if col in found:
            continue
        idx = text_lower.find(label)
        while idx != -1:
            snippet = full_text[idx: idx + 100]
            m = _BARCODE_RE.search(snippet)
            if m:
                digits = m.group(1)
                if col == "UPC" and len(digits) == 12:
                    found[col] = digits
                elif col == "EAN" and len(digits) == 13:
                    found[col] = digits
                elif col == "GTIN" and len(digits) in (12, 13, 14):
                    found[col] = digits
                elif col not in found and len(digits) in (8, 12, 13, 14):
                    found[col] = digits
            idx = text_lower.find(label, idx + 1)
    return found


def _images(soup: BeautifulSoup, base_url: str, mpn: str) -> list:
    imgs = []
    seen: set = set()
    for tag in soup.find_all("img"):
        src = (tag.get("src") or tag.get("data-src") or
               tag.get("data-lazy-src") or tag.get("data-original") or "")
        if not src:
            continue
        full = urljoin(base_url, src)
        if full in seen:
            continue
        path = urlparse(full).path.lower().split("?")[0]
        if not any(path.endswith(e) for e in _IMAGE_EXTS):
            continue
        alt = tag.get("alt", "")
        combined = (full + " " + alt).lower()
        if any(t in combined for t in _IMAGE_BAD):
            continue
        score = (10 if mpn.lower() in full.lower() else 0) + (5 if "product" in full.lower() else 0)
        imgs.append({"url": full, "score": score})
        seen.add(full)
    imgs.sort(key=lambda x: x["score"], reverse=True)
    return [i["url"] for i in imgs[:8]]


def _pdf_links(soup: BeautifulSoup, base_url: str) -> list:
    links = []
    seen: set = set()
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"])
        if full in seen:
            continue
        if ".pdf" in full.lower() or "/pdf/" in full.lower():
            label = a.get_text(" ", strip=True)
            links.append({"url": full, "label": label})
            seen.add(full)
    return links


def _features(soup: BeautifulSoup) -> list:
    """Extract bullet-point product features."""
    candidates = []
    seen: set = set()

    def _is_feature_like(text: str) -> bool:
        # A good feature is usually a phrase or sentence, not just 1-2 words like "Hardware" or "Home"
        return 20 < len(text) < 400 and " " in text.strip()

    # Common containers: .features, .product-features, ul.bullets
    for ul in soup.find_all("ul"):
        cls = " ".join(ul.get("class", []))
        # If it explicitly looks like a features list:
        if re.search(r"feature|bullet|highlight|benefit|spec", cls, re.I):
            items = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
            # But double check it's not a sneaky navigation menu
            if items and sum(1 for i in items if _is_feature_like(i)) / len(items) > 0.5:
                for text in items:
                    if text not in seen and len(text) > 5:
                        candidates.append(text)
                        seen.add(text)
                if candidates:
                    return candidates[:20]

    # Fallback: any ul whose li items look like product bullets
    if not candidates:
        for ul in soup.find_all("ul"):
            items = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
            if 3 <= len(items) <= 20:
                # Are most of these items feature-like?
                if items and sum(1 for i in items if _is_feature_like(i)) / len(items) > 0.7:
                    for text in items:
                        if text not in seen:
                            candidates.append(text)
                            seen.add(text)
                    if candidates:
                        break
    return candidates[:20]


def _description_paragraphs(soup: BeautifulSoup, mpn: str) -> list:
    """Unstructured description text — candidate for embedding."""
    skip_tags = {"nav", "footer", "header", "aside", "script", "style"}
    keywords = {
        "feature", "application", "specification", "material", "dimension",
        "weight", "grit", "voltage", "amperage", "warranty", "includes",
        "compatible", "designed", "performance", "mounting", "sound",
    }
    candidates = []
    seen: set = set()
    for tag in soup.find_all(["p", "li", "div"]):
        if any(p.name in skip_tags for p in tag.parents):
            continue
        text = tag.get_text(" ", strip=True)
        if not (60 <= len(text) <= 2000):
            continue
        tl = text.lower()
        if mpn.lower() in tl or any(kw in tl for kw in keywords):
            key = text[:100]
            if key not in seen:
                seen.add(key)
                candidates.append(text)
    return candidates[:8]   # cap per page


def parse_product_page(url: str, mpn: str, brand: str) -> dict | None:
    """
    Parse one product page.  Returns None if MPN not found on page.
    """
    print(f"    [HTML] {url[:80]}")
    soup = _fetch(url)
    if not soup:
        return None

    full_text = soup.get_text(" ", strip=True)
    mpn_norm = re.sub(r"[\s\-]", "", mpn).lower()
    text_norm = re.sub(r"[\s\-]", "", full_text).lower()
    if mpn_norm not in text_norm:
        print(f"    [HTML] MPN not found — skip")
        return None

    # Product name
    prod_name = ""
    og = soup.find("meta", property="og:title")
    if og: prod_name = og.get("content", "")
    if not prod_name:
        h1 = soup.find("h1")
        if h1: prod_name = h1.get_text(" ", strip=True)
    if not prod_name and soup.title:
        prod_name = soup.title.string or ""

    return {
        "source_url": url,
        "product_name": prod_name.strip(),
        "json_ld": _json_ld(soup),
        "specs": _spec_tables(soup),
        "barcodes": _barcodes(soup, full_text),
        "images": _images(soup, url, mpn),
        "pdf_links": _pdf_links(soup, url),
        "features": _features(soup),
        "description_paragraphs": _description_paragraphs(soup, mpn),
        "full_text_length": len(full_text),
    }


def parse_all_pages(ref_urls: list, mpn: str, brand: str,
                    mfr_url: str | None = None, max_pages: int = 5) -> list:
    """Parse up to max_pages. mfr_url is always parsed first."""
    queue = []
    if mfr_url:
        queue.append(mfr_url)
    for u in ref_urls:
        if u != mfr_url and u not in queue:
            queue.append(u)
    queue = queue[:max_pages]

    results = []
    for url in queue:
        ev = parse_product_page(url, mpn, brand)
        if ev:
            results.append(ev)
        time.sleep(CRAWL_DELAY)

    print(f"    [HTML] Parsed {len(results)}/{len(queue)} pages")
    return results
