"""
Stage 1: Resource Classifier
============================
Deterministically classifies every URL in a debug.json entry into typed buckets.
No LLM. No web requests. Pure rule-based routing.

Input:  One product entry from discovery_debug.json
Output: classified_resources dict with:
  - mfr_url
  - ref_urls (up to 5 real product pages)
  - pdf_urls (by sub-type: sds, catalog, specification, manual, product)
  - image_urls
  - video_urls
  - discard_urls (for audit)
"""

import re
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Manufacturer domain lookup: brand/manufacturer → known official domains
# Expand this list as more brands are encountered.
# ---------------------------------------------------------------------------
MANUFACTURER_DOMAINS = {
    "diablo": ["diablotools.com"],
    "3m": ["3m.com", "solutions.3m.com"],
    "frigidaire": ["frigidaire.com"],
    "whirlpool": ["whirlpool.com", "learnwhirlpool.com"],
    "freud": ["freudtools.com", "diablotools.com"],
    "milwaukee": ["milwaukeetool.com"],
    "dewalt": ["dewalt.com"],
    "stanley": ["stanleytools.com"],
    "makita": ["makitatools.com"],
    "bosch": ["boschtools.com", "bosch-professional.com"],
    "norton": ["nortonabrasives.com"],
    "mirka": ["mirka.com"],
    "festool": ["festool.com"],
}

# ---------------------------------------------------------------------------
# URL pattern rules
# ---------------------------------------------------------------------------

# Patterns that definitively mean "discard" (not a product page)
DISCARD_PATTERNS = [
    r"/catalogsearch/",
    r"/search\?",
    r"/search/",
    r"[?&]q=",
    r"[?&]query=",
    r"#(mobile-menu|nav|footer|header|menu|cart|wishlist)",
    r"/category/",
    r"/categories/",
    r"/c-\d+",
    r"/explore/[^/]+\?filters=",    # category filter pages
    r"/browse/",
    r"/sitemap",
    r"/support/downloads$",         # generic download hub (not product-specific)
    r"/product-brochures$",         # generic brochure hub
    r"/en/catalog$",                # generic catalog hub
    r"/@",                          # YouTube channel pages (not individual videos)
    r"/user/",                      # YouTube user pages
    r"/channel/",
    r"/c/[A-Za-z]",                 # YouTube channel shortlinks
    r"/@\w+$",                      # YouTube @handle
]

# PDF type classification by URL keywords
PDF_TYPE_KEYWORDS = {
    "sds": ["sds", "msds", "safety-data", "safety_data", "safetydata"],
    "catalog": ["catalog", "catalogue", "brochure"],
    "specification": ["spec", "tds", "datasheet", "data-sheet", "data_sheet", "technical"],
    "manual": ["manual", "install", "instruction", "user-guide", "user_guide", "owners", "service"],
}

# Image extensions
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"}

# Video domains
VIDEO_DOMAINS = ["youtube.com", "youtu.be", "vimeo.com"]

# Known bad image patterns (logos, icons, social)
IMAGE_DISCARD_PATTERNS = [
    "logo", "icon", "favicon", "payment", "paypal", "visa", "mastercard",
    "facebook", "twitter", "instagram", "pinterest", "social", "badge",
    "cart", "search-icon", "arrow", "star-rating", "spacer",
]


def _is_discard_url(url: str) -> bool:
    """Return True if this URL should be discarded (not a product page)."""
    url_lower = url.lower()
    for pattern in DISCARD_PATTERNS:
        if re.search(pattern, url_lower):
            return True
    return False


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _is_manufacturer_url(url: str, brand: str, manufacturer: str) -> bool:
    """Check if URL belongs to the manufacturer's official domain."""
    domain = _get_domain(url)
    # Check from known lookup
    for key in [brand.lower(), manufacturer.lower()]:
        for known_domain in MANUFACTURER_DOMAINS.get(key, []):
            if known_domain in domain:
                return True
    # Also check if debug.json flagged it as official manufacturer domain
    return False


def _classify_pdf(url: str, link_text: str = "") -> str:
    """Return pdf sub-type: sds | catalog | specification | manual | product"""
    combined = (url + " " + link_text).lower()
    for pdf_type, keywords in PDF_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                return pdf_type
    return "product"


def _is_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in IMAGE_EXTENSIONS)


def _is_bad_image(url: str, alt: str = "") -> bool:
    combined = (url + " " + alt).lower()
    return any(p in combined for p in IMAGE_DISCARD_PATTERNS)


def _is_video_url(url: str) -> bool:
    domain = _get_domain(url)
    return any(vd in domain for vd in VIDEO_DOMAINS)


def _is_valid_video(url: str) -> bool:
    """Only individual YouTube/Vimeo videos — not channel/user pages."""
    url_lower = url.lower()
    if "youtube.com/watch" in url_lower or "youtu.be/" in url_lower:
        return True
    if "vimeo.com/" in url_lower and re.search(r"/\d+", url_lower):
        return True
    return False


def _is_product_page(url: str, score: int, reasons: list) -> bool:
    """
    A URL is considered a real product page if:
    - Score >= 50 (MPN found in content)
    - Not a category/search/catalog-hub page
    - Has a product-style path (contains product identifier segment)
    """
    if score < 50:
        return False
    url_lower = url.lower()
    # Must not be a filter/category page even if MPN somehow appears
    if re.search(r"\?filters=", url_lower):
        return False
    if re.search(r"/explore/[^?]+\?", url_lower):
        return False
    return True


def classify(debug_entry: dict) -> dict:
    """
    Main classification function.

    Args:
        debug_entry: One product dict from discovery_debug.json

    Returns:
        classified_resources dict
    """
    mpn = debug_entry.get("original_mpn", debug_entry.get("mpn", ""))
    brand = debug_entry.get("resolved_brand", "")
    manufacturer = debug_entry.get("description", "")  # sometimes contains mfr name

    result = {
        "mpn": mpn,
        "brand": brand,
        "mfr_url": None,
        "ref_urls": [],          # up to 5 confirmed product pages
        "pdf_urls": {            # keyed by sub-type, each is a list of URLs
            "sds": [],
            "catalog": [],
            "specification": [],
            "manual": [],
            "product": [],
        },
        "image_urls": [],
        "video_urls": [],
        "discard_urls": [],
        "all_accepted_urls": [], # for audit
    }

    seen_urls = set()

    def add_url(url, bucket, sub=None):
        if url in seen_urls:
            return
        seen_urls.add(url)
        if bucket == "ref_urls":
            if len(result["ref_urls"]) < 5:
                result["ref_urls"].append(url)
        elif bucket == "pdf_urls":
            result["pdf_urls"][sub].append(url)
        elif bucket == "mfr_url":
            if result["mfr_url"] is None:
                result["mfr_url"] = url
        elif bucket == "image_urls":
            result["image_urls"].append(url)
        elif bucket == "video_urls":
            result["video_urls"].append(url)
        elif bucket == "discard_urls":
            result["discard_urls"].append(url)

    # ---- Walk all queries and their results ----
    for query_block in debug_entry.get("queries", []):
        for r in query_block.get("results", []):
            url = r.get("url", "")
            score = r.get("score", 0)
            status = r.get("status", "")
            reasons = r.get("reasons", [])
            title = r.get("title", "")

            if not url:
                continue

            # 1. Image URLs — never embed
            if _is_image_url(url):
                if not _is_bad_image(url):
                    add_url(url, "image_urls")
                else:
                    add_url(url, "discard_urls")
                continue

            # 2. Video URLs — only individual videos
            if _is_video_url(url):
                if _is_valid_video(url):
                    add_url(url, "video_urls")
                else:
                    add_url(url, "discard_urls")
                continue

            # 3. PDF URLs — classify by type
            if ".pdf" in url.lower() or "/pdf/" in url.lower():
                pdf_type = _classify_pdf(url, title)
                add_url(url, "pdf_urls", pdf_type)
                result["all_accepted_urls"].append(url)
                continue

            # 4. Discard patterns — before anything else
            if _is_discard_url(url):
                add_url(url, "discard_urls")
                continue

            # 5. Manufacturer URL — highest priority for mfr_url column
            is_mfr = _is_manufacturer_url(url, brand, manufacturer)
            if is_mfr or any("+30 Official manufacturer domain" in r for r in reasons):
                # Only set mfr_url if it's a product page (not a search/explore page)
                if _is_product_page(url, score, reasons):
                    add_url(url, "mfr_url")
                    result["all_accepted_urls"].append(url)
                    continue
                else:
                    # Manufacturer domain but a category page — still discard for evidence
                    add_url(url, "discard_urls")
                    continue

            # 6. Real product pages — score ≥ 50, not a category page
            if _is_product_page(url, score, reasons):
                add_url(url, "ref_urls")
                result["all_accepted_urls"].append(url)
            else:
                add_url(url, "discard_urls")

    # ---- Also check the top-level "accepted" list if present ----
    for r in debug_entry.get("accepted", []):
        url = r.get("url", "")
        score = r.get("score", 0)
        reasons = r.get("reasons", [])
        title = r.get("title", "")

        if not url or url in seen_urls:
            continue

        if _is_image_url(url):
            if not _is_bad_image(url):
                add_url(url, "image_urls")
            continue
        if _is_video_url(url):
            if _is_valid_video(url):
                add_url(url, "video_urls")
            continue
        if ".pdf" in url.lower():
            pdf_type = _classify_pdf(url, title)
            add_url(url, "pdf_urls", pdf_type)
            continue
        if _is_discard_url(url):
            add_url(url, "discard_urls")
            continue

        is_mfr = _is_manufacturer_url(url, brand, manufacturer)
        if is_mfr or any("+30 Official manufacturer domain" in r for r in reasons):
            if _is_product_page(url, score, reasons):
                add_url(url, "mfr_url")
                continue
            else:
                add_url(url, "discard_urls")
                continue

        if _is_product_page(url, score, reasons):
            add_url(url, "ref_urls")

    return result


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    debug_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "discovery_debug.json")
    with open(debug_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for entry in data:
        result = classify(entry)
        print(f"\n{'='*60}")
        print(f"MPN: {result['mpn']}  Brand: {result['brand']}")
        print(f"  MFR URL:   {result['mfr_url']}")
        print(f"  Ref URLs:  {len(result['ref_urls'])} → {result['ref_urls'][:2]}")
        print(f"  PDFs:      {sum(len(v) for v in result['pdf_urls'].values())} → {result['pdf_urls']}")
        print(f"  Images:    {len(result['image_urls'])}")
        print(f"  Videos:    {result['video_urls']}")
        print(f"  Discarded: {len(result['discard_urls'])}")
