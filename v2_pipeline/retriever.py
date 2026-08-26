"""
Stage 6 — Schema-driven Retriever
===================================
Fires one Voyage query per schema field group, retrieves top-3 chunks each,
de-duplicates across groups. Total: up to 15 unique chunks per product.

Groups (aligned to 252-column schema):
  identity   → MPN, brand, UPC/EAN/GTIN, UNSPSC, alternate part number
  physical   → dimensions, weight, volume, size
  attributes → grit, voltage, material, color, RPM … (ATTRIBUTE_LABEL columns)
  features   → ITEM_FEATURES_*, APPLICATION, WITH, INCLUDES
  compliance → SDS, RoHS, Prop 65, warranty, certifications, country of origin
"""

from __future__ import annotations
import os
import time
import requests

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

VOYAGE_API_KEYS = [k.strip() for k in os.getenv("VOYAGE_API_KEY", "").split(",") if k.strip()]
_voyage_key_idx = 0
EMBED_MODEL     = "voyage-3"
COLLECTION_NAME = "evidence_v2"
TOP_K           = 3          # chunks per group
_RETRY_DELAYS   = [1] * 25

_GROUPS: list[tuple[str, str]] = [
    (
        "identity",
        "{mpn} {brand} manufacturer part number SKU alternate UPC EAN GTIN UNSPSC barcode item number",
    ),
    (
        "physical",
        "{mpn} length width height diameter depth thickness weight volume size inches mm feet",
    ),
    (
        "attributes",
        "{mpn} specifications material grit voltage amperage watt color capacity speed RPM OPM "
        "backing grain abrasive mineral coating mounting series model application type",
    ),
    (
        "features",
        "{mpn} features benefits application includes designed for compatible performance "
        "description marketing what is in the box",
    ),
    (
        "compliance",
        "{mpn} SDS safety MSDS RoHS Prop 65 REACH certification UL listed ENERGY STAR "
        "warranty guarantee country of origin compliance",
    ),
]


def _get_voyage_key():
    return VOYAGE_API_KEYS[_voyage_key_idx] if VOYAGE_API_KEYS else ""

def _rotate_voyage_key():
    global _voyage_key_idx
    if len(VOYAGE_API_KEYS) > 1:
        _voyage_key_idx = (_voyage_key_idx + 1) % len(VOYAGE_API_KEYS)
        print(f"    [Voyage-query] Switched to API Key index {_voyage_key_idx}")

def _embed_query(text: str) -> list | None:
    if not VOYAGE_API_KEYS:
        raise ValueError("VOYAGE_API_KEY not set")
    url     = "https://api.voyageai.com/v1/embeddings"
    payload = {"input": [text], "model": EMBED_MODEL, "input_type": "query"}

    for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
        api_key = _get_voyage_key()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                return r.json()["data"][0]["embedding"]
            elif r.status_code in [429, 401, 403]:
                if r.status_code == 429:
                    print(f"    [Voyage-query] 429 Rate Limit")
                else:
                    print(f"    [Voyage-query] Auth Error {r.status_code}")
                _rotate_voyage_key()
                if delay is None: return None
                pass # No wait, immediately use next key
            else:
                print(f"    [Voyage-query] {r.status_code}: {r.text[:100]}")
                if delay is None: return None
                time.sleep(5)
        except Exception as e:
            print(f"    [Voyage-query] Exception: {e}")
            if delay is None: return None
            time.sleep(5)
    return None


def retrieve_for_product(
    client: QdrantClient,
    mpn: str,
    brand: str,
) -> tuple[list, dict]:
    """
    Returns (unique_chunks_list, retrieval_stats_dict).
    """
    print(f"    [Retrieval] Schema-group retrieval for {mpn}")
    mpn_filter = Filter(must=[FieldCondition(key="mpn", match=MatchValue(value=mpn))])

    all_chunks: list[dict] = []
    seen_texts: set[str]   = set()
    stats: dict = {
        "groups_queried": 0,
        "chunks_per_group": {},
        "total_unique_chunks": 0,
        "query_embed_failures": 0,
    }

    for group_name, template in _GROUPS:
        query = template.format(mpn=mpn, brand=brand)
        vec   = _embed_query(query)
        if vec is None:
            print(f"    [Retrieval] Group '{group_name}' embed failed — skip")
            stats["query_embed_failures"] += 1
            stats["chunks_per_group"][group_name] = 0
            continue

        try:
            res    = client.query_points(
                collection_name=COLLECTION_NAME,
                query=vec,
                query_filter=mpn_filter,
                limit=TOP_K,
            )
            points = res.points
        except Exception as e:
            print(f"    [Retrieval] Qdrant error ({group_name}): {e}")
            stats["chunks_per_group"][group_name] = 0
            continue

        added = 0
        for pt in points:
            text_key = pt.payload.get("text", "")[:120]
            if text_key not in seen_texts:
                seen_texts.add(text_key)
                all_chunks.append(pt.payload)
                added += 1

        stats["chunks_per_group"][group_name] = added
        stats["groups_queried"] += 1
        time.sleep(0.3)   # small pause between group queries

    stats["total_unique_chunks"] = len(all_chunks)
    print(
        f"    [Retrieval] {stats['total_unique_chunks']} unique chunks "
        f"from {stats['groups_queried']} groups"
    )
    return all_chunks, stats
