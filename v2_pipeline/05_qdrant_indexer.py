"""
Stage 5: Qdrant Indexer
=======================
Embeds chunk candidates using Voyage AI and upserts into a per-run Qdrant collection.

Key differences from previous pipeline:
  - Batch size: 20 (not 72) — stays well under rate limits
  - Retry: exponential backoff 15s → 30s → 60s (not fixed 65s)
  - Max 20 chunks per product (already enforced by Stage 4)
  - Each chunk has schema-group metadata for targeted retrieval
  - Collection is created fresh for each 5-row test run (clean state)

Each vector payload stores:
  {
    "mpn": str,
    "brand": str,
    "source_type": str,   # html_description | pdf_sds | pdf_catalog | ...
    "source_url": str,
    "text": str,
    "field_groups": [str] # which schema groups this chunk likely covers
  }
"""

import os
import re
import time
import json
import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct, VectorParams, Distance, Filter,
    FieldCondition, MatchValue
)

VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = "voyage-3"
VECTOR_DIM = 1024   # voyage-3 output dimension
BATCH_SIZE = 20     # stay well under Voyage rate limits

# Retry schedule (seconds) for 429 rate limit responses
RETRY_DELAYS = [15, 30, 60, 120]

# Schema field groups for metadata tagging
# Each group lists keywords; if a chunk's text contains any, it gets that group tag
FIELD_GROUP_KEYWORDS = {
    "identity": ["mpn", "part number", "sku", "model", "item number", "brand",
                 "manufacturer", "trade name", "upc", "ean", "gtin"],
    "physical": ["dimension", "length", "width", "height", "diameter",
                 "weight", "volume", "size", "depth", "thickness"],
    "attributes": ["grit", "voltage", "amperage", "watt", "material",
                   "color", "colour", "capacity", "speed", "rpm", "opm",
                   "backing", "grain", "abrasive", "coating"],
    "features": ["feature", "benefit", "advantage", "application", "designed for",
                 "compatible", "ideal for", "use with", "performance"],
    "compliance": ["sds", "safety", "msds", "rohs", "prop 65", "reach", "ul listed",
                   "energy star", "ce mark", "fcc", "warranty", "certified"],
}


def _tag_field_groups(text: str) -> list:
    """Tag which schema field groups a chunk likely covers."""
    text_lower = text.lower()
    groups = []
    for group, keywords in FIELD_GROUP_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            groups.append(group)
    return groups if groups else ["general"]


def _embed_batch(texts: list) -> list | None:
    """
    Call Voyage AI to embed a batch of texts.
    Returns list of embedding vectors, or None on persistent failure.
    """
    if not VOYAGE_API_KEY:
        raise ValueError("VOYAGE_API_KEY not set. Please set the environment variable.")

    url = "https://api.voyageai.com/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {VOYAGE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": texts,
        "model": EMBEDDING_MODEL,
        "input_type": "document",
    }

    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                return [d["embedding"] for d in resp.json()["data"]]
            elif resp.status_code == 429:
                if delay is None:
                    print(f"    [Voyage] Rate limit — all retries exhausted")
                    return None
                print(f"    [Voyage] Rate limit (429) — waiting {delay}s (attempt {attempt+1})")
                time.sleep(delay)
            else:
                print(f"    [Voyage] Error {resp.status_code}: {resp.text[:200]}")
                if delay is None:
                    return None
                time.sleep(min(delay, 15))
        except Exception as e:
            print(f"    [Voyage] Request exception: {e}")
            if delay is None:
                return None
            time.sleep(10)

    return None


def get_or_create_collection(client: QdrantClient, collection_name: str = "evidence_v2") -> None:
    """Create Qdrant collection if it doesn't exist."""
    try:
        client.get_collection(collection_name)
        print(f"    [Qdrant] Using existing collection: {collection_name}")
    except Exception:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
        print(f"    [Qdrant] Created new collection: {collection_name}")


def embed_and_index(
    client: QdrantClient,
    mpn: str,
    brand: str,
    chunk_candidates: list,
    collection_name: str = "evidence_v2",
    id_offset: int = 0,
) -> dict:
    """
    Embed chunk_candidates and upsert into Qdrant.

    Args:
        client:           QdrantClient instance
        mpn:              Product MPN (used for payload + filtering)
        brand:            Brand name
        chunk_candidates: List of {"text": str, "source_type": str, "source_url": str}
        collection_name:  Qdrant collection name
        id_offset:        Starting point ID (to avoid collisions across products)

    Returns:
        stats dict: chunks_embedded, chunks_failed, points_upserted
    """
    if not chunk_candidates:
        print(f"    [Qdrant] No chunks to embed for {mpn}")
        return {"chunks_embedded": 0, "chunks_failed": 0, "points_upserted": 0}

    print(f"    [Qdrant] Embedding {len(chunk_candidates)} chunks for {mpn}")

    points = []
    chunks_embedded = 0
    chunks_failed = 0

    for i in range(0, len(chunk_candidates), BATCH_SIZE):
        batch = chunk_candidates[i: i + BATCH_SIZE]
        texts = [c["text"] for c in batch]

        print(f"    [Qdrant] Embedding batch {i+1}–{i+len(batch)} of {len(chunk_candidates)}")
        vectors = _embed_batch(texts)

        if vectors is None:
            print(f"    [Qdrant] Batch failed — skipping {len(batch)} chunks")
            chunks_failed += len(batch)
            continue

        for j, (chunk, vec) in enumerate(zip(batch, vectors)):
            point_id = id_offset + i + j + 1
            payload = {
                "mpn": mpn,
                "brand": brand,
                "source_type": chunk.get("source_type", "unknown"),
                "source_url": chunk.get("source_url", ""),
                "text": chunk["text"],
                "field_groups": _tag_field_groups(chunk["text"]),
                "is_ocr": chunk.get("is_ocr", False),
            }
            points.append(PointStruct(id=point_id, vector=vec, payload=payload))
            chunks_embedded += 1

        # Small pause between batches
        if i + BATCH_SIZE < len(chunk_candidates):
            time.sleep(1)

    # Upsert all points
    if points:
        client.upsert(collection_name=collection_name, points=points)
        print(f"    [Qdrant] Upserted {len(points)} points for {mpn}")

    return {
        "chunks_embedded": chunks_embedded,
        "chunks_failed": chunks_failed,
        "points_upserted": len(points),
    }


def delete_product_vectors(
    client: QdrantClient, mpn: str, collection_name: str = "evidence_v2"
) -> None:
    """Delete all vectors for a given MPN (useful for re-runs)."""
    try:
        client.delete(
            collection_name=collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="mpn", match=MatchValue(value=mpn))]
            ),
        )
        print(f"    [Qdrant] Deleted existing vectors for {mpn}")
    except Exception as e:
        print(f"    [Qdrant] Could not delete existing vectors for {mpn}: {e}")
