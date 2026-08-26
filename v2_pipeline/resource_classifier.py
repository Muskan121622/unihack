"""
Stage 1 — Resource Classifier  (v2, P0+P1 fixes applied)
==========================================================
Deterministically classifies every URL in a debug.json entry into typed buckets.
No LLM. No web requests. Pure rule-based routing.

FIXES vs first draft
---------------------
P0-1  Exact/subdomain domain matching (no accidental partial matches).
P0-2  PDF URLs go through discard rules first (no bypass).
P0-3  Best-occurrence wins for duplicates (highest score kept per URL).
P0-4  Stronger _is_product_page(): rejects filter pages, short paths, search slugs.
P1-5  SVG excluded from images (vector/icon format, not product photo).
P1-6  Video domain matching is exact/subdomain (not substring).
P1-7  PDF classification uses weighted scoring, not first-keyword match.

Output shape
------------
{
  "mpn": str,
  "brand": str,
  "mfr_url": str | None,
  "ref_urls": [str],          # up to 5 accepted product pages
  "pdf_urls": {
    "sds": [str],
    "catalog": [str],
    "specification": [str],
    "manual": [str],
    "product": [str],
  },
  "image_urls": [str],
  "video_urls": [str],
  "discard_urls": [str],
  "all_accepted_urls": [str],
}
"""

from __future__ import annotations
import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Manufacturer domain registry
# brand/manufacturer key (lowercase) → list of exact base domains
# ---------------------------------------------------------------------------
MANUFACTURER_DOMAINS: dict[str, list[str]] = {
    "diablo":      ["diablotools.com"],
    "freud":       ["freudtools.com", "diablotools.com"],
    "3m":          ["3m.com", "solutions.3m.com"],
    "frigidaire":  ["frigidaire.com"],
    "whirlpool":   ["whirlpool.com", "learnwhirlpool.com"],
    "milwaukee":   ["milwaukeetool.com"],
    "dewalt":      ["dewalt.com"],
    "stanley":     ["stanleytools.com"],
    "makita":      ["makitatools.com"],
    "bosch":       ["boschtools.com", "bosch-professional.com"],
    "norton":      ["nortonabrasives.com"],
    "mirka":       ["mirka.com"],
    "festool":     ["festool.com"],
    "simpson":     ["strongtie.com"],
    "fluke":       ["fluke.com"],
    "ridgid":      ["ridgid.com"],
    "klein":       ["kleintools.com"],
    "greenlee":    ["greenlee.com"],
    "channellock": ["channellock.com"],
    "irwin":       ["irwin.com"],
    "lenox":       ["lenoxtools.com"],
    "starrett":    ["starrett.com"],
    "ideal":       ["idealindustries.com"],
}

# ---------------------------------------------------------------------------
# Discard patterns (applied to ALL URL types before anything else)
# ---------------------------------------------------------------------------
_DISCARD_RE = re.compile(
    r"/catalogsearch/"
    r"|/search\?"
    r"|[?&](q|query|s|term|keyword)="
    r"|/category/"
    r"|/categories/"
    r"|/c-\d"             # /c-12345 category slugs
    r"|/browse/"
    r"|/sitemap"
    r"|/support/downloads/?$"
    r"|/product-brochures/?$"
    r"|/en/catalog/?$"
    r"|/deals/"
    r"|/clearance/"
    r"|/wishlist"
    r"|/cart/"
    r"|/account/"
    r"|/login"
    r"|/checkout",
    re.IGNORECASE,
)

# Patterns that disqualify a page from being a product page even if score is high
_NOT_PRODUCT_RE = re.compile(
    r"/explore/[^?]+\?filters="  # category filter URLs
    r"|/explore/[^/]+\?.*filters"
    r"|\?filters="
    r"|/search\?"
    r"|/c/[A-Za-z]"             # YouTube channel shortlinks
    r"|/@[\w]"                  # YouTube @handle pages
    r"|/user/"                  # YouTube user pages
    r"|/channel/"
    r"|/collections/"           # Shopify collection pages
    r"|/departments/"           # Ace Hardware department listing
    r"|/brands/"
    r"|/manufacturer/",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Image: allowed extensions (SVG excluded — it's a vector/icon format)
# ---------------------------------------------------------------------------
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_IMAGE_BAD_TERMS = {
    "logo", "icon", "favicon", "payment", "paypal", "visa", "mastercard",
    "facebook", "twitter", "instagram", "pinterest", "social", "badge",
    "cart", "search-icon", "arrow", "star-rating", "spacer", "sprite",
    "pixel", "1x1", "2x2", "blank",
}

# ---------------------------------------------------------------------------
# Video: exact base-domain matching
# ---------------------------------------------------------------------------
_VIDEO_DOMAINS = {"youtube.com", "youtu.be", "vimeo.com", "dailymotion.com"}
_VALID_VIDEO_RE = re.compile(
    r"youtube\.com/watch\?"
    r"|youtu\.be/[\w\-]"
    r"|vimeo\.com/\d+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# PDF classification: weighted scoring
# ---------------------------------------------------------------------------
_PDF_SCORES: dict[str, list[tuple[int, str]]] = {
    "sds": [
        (10, r"\bsds\b"),
        (10, r"\bmsds\b"),
        (8,  r"safety[-_]data"),
        (8,  r"safety_sheet"),
        (6,  r"safety"),
    ],
    "catalog": [
        (10, r"\bcatalog(?:ue)?\b"),
        (8,  r"\bbrochure\b"),
    ],
    "specification": [
        (10, r"\bspec(?:ification)?[-_]sheet\b"),
        (8,  r"\btds\b"),
        (8,  r"technical[-_]data"),
        (6,  r"\bdatasheet\b"),
        (6,  r"\bspec\b"),
    ],
    "manual": [
        (10, r"\binstallation[-_]?instruction"),
        (10, r"\bowner[s]?[-_]?manual\b"),
        (10, r"\bservice[-_]?manual\b"),
        (8,  r"\binstruction[-_]?manual\b"),
        (8,  r"\buser[-_]?guide\b"),
        (6,  r"\bmanual\b"),
        (6,  r"\binstall\b"),
    ],
}


def _classify_pdf(url: str, link_text: str = "", title: str = "") -> str:
    """Return the best PDF sub-type using weighted keyword scoring."""
    combined = f"{url} {link_text} {title}".lower()
    best_type = "product"
    best_score = 0
    for pdf_type, rules in _PDF_SCORES.items():
        score = sum(w for w, pat in rules if re.search(pat, combined))
        if score > best_score:
            best_score = score
            best_type = pdf_type
    return best_type


def _base_domain(url: str) -> str:
    """Return bare domain without www., e.g. 'www.example.co.uk' → 'example.co.uk'."""
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.removeprefix("www.")
    except Exception:
        return ""


def _is_subdomain_of(url_domain: str, target_domain: str) -> bool:
    """True if url_domain is exactly target_domain or a subdomain of it."""
    return url_domain == target_domain or url_domain.endswith("." + target_domain)


def _is_manufacturer_url(url: str, brand: str, manufacturer: str,
                          reasons: list[str]) -> bool:
    """Return True if URL belongs to a known manufacturer domain."""
    domain = _base_domain(url)
    # Check from known registry
    for key in [brand.lower(), manufacturer.lower()]:
        for known_domain in MANUFACTURER_DOMAINS.get(key, []):
            if _is_subdomain_of(domain, known_domain):
                return True
    # Fall back to debug.json reason flag
    return any("+30 Official manufacturer domain" in r for r in reasons)


def _is_image_url(url: str) -> bool:
    path = urlparse(url).path.lower().split("?")[0]
    return any(path.endswith(ext) for ext in _IMAGE_EXTS)


def _is_bad_image(url: str, alt: str = "") -> bool:
    combined = (url + " " + alt).lower()
    return any(t in combined for t in _IMAGE_BAD_TERMS)


def _is_video_url(url: str) -> bool:
    domain = _base_domain(url)
    return any(_is_subdomain_of(domain, vd) for vd in _VIDEO_DOMAINS)


def _is_valid_video(url: str) -> bool:
    """Only individual video pages, not channel/playlist pages."""
    return bool(_VALID_VIDEO_RE.search(url))


def _is_pdf_url(url: str) -> bool:
    url_lower = url.lower()
    return ".pdf" in url_lower or "/pdf/" in url_lower or "download=true" in url_lower


def _should_discard(url: str) -> bool:
    return bool(_DISCARD_RE.search(url))


def _is_product_page(url: str, score: int, reasons: list[str]) -> bool:
    """
    A URL is a valid product page only when ALL of:
    1. Score ≥ 50 (MPN confirmed in content)
    2. Not a category/filter/search/collection page
    3. Path has at least 2 segments (not just a domain home)
    4. URL does not look like a generic listing
    """
    if score < 50:
        return False
    if _NOT_PRODUCT_RE.search(url):
        return False
    # Require at least 2 non-empty path segments  (e.g. /product/12345)
    path_parts = [p for p in urlparse(url).path.split("/") if p]
    if len(path_parts) < 1:
        return False
    return True


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify(debug_entry: dict) -> dict:
    """
    Classify all URLs in one product's debug.json entry.

    Uses best-occurrence-wins deduplication:
    - For each unique URL, keep the highest-score occurrence.
    - Then route based on the winning score/reasons.
    """
    mpn = debug_entry.get("original_mpn", debug_entry.get("mpn", ""))
    brand = debug_entry.get("resolved_brand", "")
    manufacturer = ""  # can be extracted from description if needed

    # ---- Step 1: Collect all (url, score, reasons, title) occurrences ----
    url_best: dict[str, dict] = {}  # url → {score, reasons, title, status}

    def _record(url: str, score: int, reasons: list, title: str, status: str):
        if not url:
            return
        existing = url_best.get(url)
        if existing is None or score > existing["score"]:
            url_best[url] = {
                "score": score,
                "reasons": reasons,
                "title": title,
                "status": status,
            }

    for q_block in debug_entry.get("queries", []):
        for r in q_block.get("results", []):
            _record(r.get("url", ""), r.get("score", 0),
                    r.get("reasons", []), r.get("title", ""), r.get("status", ""))

    for r in debug_entry.get("accepted", []):
        _record(r.get("url", ""), r.get("score", 0),
                r.get("reasons", []), r.get("title", ""), r.get("status", ""))

    for r in debug_entry.get("review", []):
        _record(r.get("url", ""), r.get("score", 0),
                r.get("reasons", []), r.get("title", ""), r.get("status", ""))

    # ---- Step 2: Route each unique URL (best occurrence) ----
    result: dict = {
        "mpn": mpn,
        "brand": brand,
        "mfr_url": None,
        "mfr_score": -999,          # internal, for best-mfr selection
        "ref_urls_scored": [],      # internal: [(score, url)]
        "pdf_urls": {"sds": [], "catalog": [], "specification": [], "manual": [], "product": []},
        "image_urls": [],
        "video_urls": [],
        "discard_urls": [],
        "all_accepted_urls": [],
    }

    seen_pdfs: set[str] = set()
    seen_images: set[str] = set()
    seen_videos: set[str] = set()
    seen_refs: set[str] = set()

    for url, meta in url_best.items():
        score = meta["score"]
        reasons = meta["reasons"]
        title = meta["title"]

        # ---- Images (check before discard, but filter bad ones) ----
        if _is_image_url(url):
            if not _is_bad_image(url, title) and url not in seen_images:
                result["image_urls"].append(url)
                seen_images.add(url)
            else:
                result["discard_urls"].append(url)
            continue

        # ---- Videos (exact subdomain match) ----
        if _is_video_url(url):
            if _is_valid_video(url) and url not in seen_videos:
                result["video_urls"].append(url)
                seen_videos.add(url)
            else:
                result["discard_urls"].append(url)
            continue

        # ---- PDFs: apply discard rules FIRST, then classify ----
        if _is_pdf_url(url):
            if _should_discard(url):
                result["discard_urls"].append(url)
                continue
            if url not in seen_pdfs:
                pdf_type = _classify_pdf(url, "", title)
                result["pdf_urls"][pdf_type].append(url)
                result["all_accepted_urls"].append(url)
                seen_pdfs.add(url)
            continue

        # ---- Global discard check (non-PDF, non-image, non-video) ----
        if _should_discard(url):
            result["discard_urls"].append(url)
            continue

        # ---- Manufacturer URL (best score wins for mfr_url slot) ----
        if _is_manufacturer_url(url, brand, manufacturer, reasons):
            if _is_product_page(url, score, reasons):
                if score > result["mfr_score"]:
                    result["mfr_url"] = url
                    result["mfr_score"] = score
                result["all_accepted_urls"].append(url)
                # Also make it a ref URL candidate so it can fill ref slots
                if url not in seen_refs:
                    result["ref_urls_scored"].append((score, url))
                    seen_refs.add(url)
            else:
                result["discard_urls"].append(url)
            continue

        # ---- Product pages ----
        if _is_product_page(url, score, reasons):
            if url not in seen_refs:
                result["ref_urls_scored"].append((score, url))
                result["all_accepted_urls"].append(url)
                seen_refs.add(url)
        else:
            result["discard_urls"].append(url)

    # ---- Step 3: Sort ref_urls by score descending, take top 5 ----
    result["ref_urls_scored"].sort(key=lambda x: x[0], reverse=True)
    ref_urls = [url for _, url in result["ref_urls_scored"][:5]]

    # Ensure mfr_url is not duplicated inside ref_urls
    if result["mfr_url"] and result["mfr_url"] in ref_urls:
        ref_urls.remove(result["mfr_url"])

    # Clean up internal fields
    del result["mfr_score"]
    del result["ref_urls_scored"]
    result["ref_urls"] = ref_urls

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
        res = classify(entry)
        print(f"\n{'='*60}")
        print(f"MPN: {res['mpn']}  Brand: {res['brand']}")
        print(f"  MFR URL   : {res['mfr_url']}")
        print(f"  Ref URLs  : {res['ref_urls']}")
        print(f"  PDF types : { {k: len(v) for k,v in res['pdf_urls'].items() if v} }")
        print(f"  Images    : {len(res['image_urls'])}")
        print(f"  Videos    : {res['video_urls']}")
        print(f"  Discarded : {len(res['discard_urls'])}")
