"""
Stage 6: Retriever
==================
Schema-driven Qdrant retrieval.

Instead of one giant query (hoping it covers all 252 columns),
fires 5 separate queries — one per schema group — each targeting top-3 chunks.
Results are de-duplicated and returned as a flat list of chunks.

Groups:
  1. identity      → MPN, brand, manufacturer, UPC, EAN, GTIN, UNSPSC
  2. physical      → dimensions, weight, diameter, volume, size
  3. attributes    → grit, voltage, material, color, capacity, speed, etc.
  4. features      → features, benefits, applications, description
  5. compliance    → SDS, safety, RoHS, certifications, warranty

Total retrieved: up to 15 unique chunks (5 groups × 3 each, de-duplicated).
This is targeted and efficient vs. fetching 15 random chunks.
"""

import os
import requests
import time

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = "voyage-3"
RETRY_DELAYS = [15, 30, 60]

TOP_K_PER_GROUP = 3  # retrieve this many chunks per schema group

# Schema group query templates
# Format: (group_name, query_template)
# {mpn} and {brand} are substituted at runtime
SCHEMA_GROUP_QUERIES = [
    (
        "identity",
        "{mpn} {brand} manufacturer part number SKU UPC EAN GTIN barcode UNSPSC alternate",
    ),
    (
        "physical",
        "{mpn} dimensions length width height diameter thickness weight volume size inches mm",
    ),
    (
        "attributes",
        "{mpn} specifications material grit voltage amperage watt color capacity "
        "speed RPM OPM backing grain abrasive mineral coating grade",
    ),
    (
        "features",
        "{mpn} features benefits application designed for compatible ideal use performance "
        "description marketing includes what comes in the box",
    ),
    (
        "compliance",
        "{mpn} SDS safety data sheet MSDS RoHS Prop 65 certification UL listed "
        "ENERGY STAR warranty guarantee country of origin",
    ),
]


def _embed_query(text: str) -> list | None:
    """Embed a single query text via Voyage AI."""
    if not VOYAGE_API_KEY:
        raise ValueError("VOYAGE_API_KEY not set.")

    url = "https://api.voyageai.com/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {VOYAGE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": [text],
        "model": EMBEDDING_MODEL,
        "input_type": "query",
    }

    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                return resp.json()["data"][0]["embedding"]
            elif resp.status_code == 429:
                if delay is None:
                    print(f"    [Voyage] Query rate limit — all retries exhausted")
                    return None
                print(f"    [Voyage] Query rate limit (429) — waiting {delay}s")
                time.sleep(delay)
            else:
                print(f"    [Voyage] Query embed error {resp.status_code}: {resp.text[:150]}")
                if delay is None:
                    return None
                time.sleep(5)
        except Exception as e:
            print(f"    [Voyage] Query embed exception: {e}")
            if delay is None:
                return None
            time.sleep(5)

    return None


def retrieve_for_product(
    client: QdrantClient,
    mpn: str,
    brand: str,
    collection_name: str = "evidence_v2",
) -> tuple[list, dict]:
    """
    Run schema-group retrieval for a product.

    Returns:
        (chunks, retrieval_stats)
        chunks: list of payload dicts from Qdrant, de-duplicated
        retrieval_stats: per-group counts + total
    """
    print(f"    [Retrieval] Schema-group retrieval for {mpn}")

    all_chunks = []
    seen_texts = set()  # for de-duplication
    stats = {
        "groups_queried": 0,
        "chunks_per_group": {},
        "total_chunks_retrieved": 0,
        "total_unique_chunks": 0,
        "query_embed_failures": 0,
    }

    mpn_filter = Filter(
        must=[FieldCondition(key="mpn", match=MatchValue(value=mpn))]
    )

    for group_name, query_template in SCHEMA_GROUP_QUERIES:
        query_text = query_template.format(mpn=mpn, brand=brand)

        vector = _embed_query(query_text)
        if vector is None:
            print(f"    [Retrieval] Group '{group_name}' — embed failed, skipping")
            stats["query_embed_failures"] += 1
            stats["chunks_per_group"][group_name] = 0
            continue

        try:
            results = client.query_points(
                collection_name=collection_name,
                query=vector,
                query_filter=mpn_filter,
                limit=TOP_K_PER_GROUP,
            )
            group_chunks = results.points
        except Exception as e:
            print(f"    [Retrieval] Qdrant query error for group '{group_name}': {e}")
            stats["chunks_per_group"][group_name] = 0
            continue

        group_unique = 0
        for point in group_chunks:
            payload = point.payload
            text = payload.get("text", "")
            text_key = text[:120]  # de-dup by first 120 chars
            if text_key not in seen_texts:
                seen_texts.add(text_key)
                all_chunks.append(payload)
                group_unique += 1

        stats["chunks_per_group"][group_name] = group_unique
        stats["groups_queried"] += 1
        stats["total_chunks_retrieved"] += len(group_chunks)

        # Small pause between group queries (Voyage rate limit safety)
        time.sleep(0.3)

    stats["total_unique_chunks"] = len(all_chunks)
    print(
        f"    [Retrieval] Retrieved {stats['total_unique_chunks']} unique chunks "
        f"across {stats['groups_queried']} groups"
    )
    return all_chunks, stats
