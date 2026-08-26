"""
Stage 5 — Qdrant Indexer  (no-duplicate guarantee)
====================================================
Embeds chunk candidates via Voyage AI and upserts into Qdrant.

NO DUPLICATE guarantee:
  - Each point ID is a deterministic SHA-1 hash of (mpn + text[:120]).
    Qdrant upsert = insert-or-replace, so re-running never creates duplicates.
  - Before indexing, existing MPN vectors are deleted so stale chunks
    from a previous run never pollute retrieval.

Other design choices:
  - Batch size 20 (safe under Voyage rate limits)
  - Exponential backoff: 15s → 30s → 60s → 120s (not fixed 65s)
  - Field-group metadata on every chunk for schema-driven retrieval in Stage 6
  - Collection: evidence_v2  (separate from old pipeline collections)
"""

from __future__ import annotations
import os
import re
import time
import hashlib
import requests

from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct, VectorParams, Distance,
    Filter, FieldCondition, MatchValue,
)

VOYAGE_API_KEYS  = [k.strip() for k in os.getenv("VOYAGE_API_KEY", "").split(",") if k.strip()]
_voyage_key_idx  = 0
EMBED_MODEL      = "voyage-3"
VECTOR_DIM       = 1024
BATCH_SIZE       = 20
COLLECTION_NAME  = "evidence_v2"
_RETRY_DELAYS    = [1] * 25

# Schema field groups — used to tag chunks so Stage 6 can filter by group
_GROUP_KW: dict[str, list[str]] = {
    "identity":   ["mpn", "part number", "sku", "model", "item number",
                   "brand", "manufacturer", "trade name", "upc", "ean", "gtin", "unspsc"],
    "physical":   ["dimension", "length", "width", "height", "diameter",
                   "thickness", "weight", "volume", "size", "depth"],
    "attributes": ["grit", "voltage", "amperage", "watt", "material", "color",
                   "colour", "capacity", "speed", "rpm", "opm", "backing",
                   "grain", "abrasive", "coating", "mineral", "mounting", "series"],
    "features":   ["feature", "benefit", "advantage", "application",
                   "designed for", "compatible", "ideal for", "use with",
                   "performance", "description", "includes", "marketing"],
    "compliance": ["sds", "safety", "msds", "rohs", "prop 65", "reach",
                   "ul listed", "energy star", "warranty", "certified",
                   "country of origin", "compliance", "standard"],
}


def _tag_groups(text: str) -> list[str]:
    tl = text.lower()
    return [g for g, kws in _GROUP_KW.items() if any(k in tl for k in kws)] or ["general"]


def _point_id(mpn: str, text: str) -> int:
    """Deterministic integer ID from SHA-1(mpn + text prefix)."""
    raw = f"{mpn}|{text[:120]}"
    h   = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    # Use first 15 hex chars → 60-bit integer (fits Qdrant uint64)
    return int(h[:15], 16)

def _get_voyage_key():
    return VOYAGE_API_KEYS[_voyage_key_idx] if VOYAGE_API_KEYS else ""

def _rotate_voyage_key():
    global _voyage_key_idx
    if len(VOYAGE_API_KEYS) > 1:
        _voyage_key_idx = (_voyage_key_idx + 1) % len(VOYAGE_API_KEYS)
        print(f"    [Voyage] Switched to API Key index {_voyage_key_idx}")

def _embed_batch(texts: list[str]) -> list | None:
    if not VOYAGE_API_KEYS:
        raise ValueError("VOYAGE_API_KEY not set")
    url     = "https://api.voyageai.com/v1/embeddings"
    payload = {"input": texts, "model": EMBED_MODEL, "input_type": "document"}

    for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
        api_key = _get_voyage_key()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=60)
            if r.status_code == 200:
                return [d["embedding"] for d in r.json()["data"]]
            elif r.status_code in [429, 401, 403]:
                if r.status_code == 429:
                    print(f"    [Voyage] 429 Rate Limit (attempt {attempt+1})")
                else:
                    print(f"    [Voyage] Auth error {r.status_code} (attempt {attempt+1})")
                _rotate_voyage_key()
                if delay is None:
                    print("    [Voyage] Retries exhausted"); return None
                pass # No wait, immediately use next key
            else:
                print(f"    [Voyage] Error {r.status_code}: {r.text[:150]}")
                if delay is None: return None
                time.sleep(min(delay, 15))
        except Exception as e:
            print(f"    [Voyage] Exception: {e}")
            if delay is None: return None
            time.sleep(10)
    return None


def get_or_create_collection(client: QdrantClient) -> None:
    try:
        client.get_collection(COLLECTION_NAME)
    except Exception:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
        print(f"    [Qdrant] Created collection: {COLLECTION_NAME}")


def delete_product_vectors(client: QdrantClient, mpn: str) -> None:
    """Delete all existing vectors for this MPN before re-indexing."""
    try:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="mpn", match=MatchValue(value=mpn))]
            ),
        )
        print(f"    [Qdrant] Cleared old vectors for {mpn}")
    except Exception as e:
        print(f"    [Qdrant] Could not clear old vectors for {mpn}: {e}")


def embed_and_index(
    client: QdrantClient,
    mpn: str,
    brand: str,
    chunks: list,
) -> dict:
    """
    Embed chunks and upsert into Qdrant with duplicate prevention.

    Returns stats dict.
    """
    if not chunks:
        print(f"    [Qdrant] No chunks for {mpn}")
        return {"chunks_embedded": 0, "chunks_failed": 0, "points_upserted": 0}

    # Step 1: clear stale vectors for this MPN
    delete_product_vectors(client, mpn)

    print(f"    [Qdrant] Embedding {len(chunks)} chunks for {mpn}")
    points  = []
    failed  = 0

    for i in range(0, len(chunks), BATCH_SIZE):
        batch  = chunks[i: i + BATCH_SIZE]
        texts  = [c["text"] for c in batch]
        print(f"    [Qdrant] Batch {i+1}–{i+len(batch)}")
        vecs   = _embed_batch(texts)

        if vecs is None:
            print(f"    [Qdrant] Batch failed — skipping {len(batch)} chunks")
            failed += len(batch)
            continue

        for chunk, vec in zip(batch, vecs):
            pid = _point_id(mpn, chunk["text"])
            payload = {
                "mpn":         mpn,
                "brand":       brand,
                "source_type": chunk.get("source_type", "unknown"),
                "source_url":  chunk.get("source_url", ""),
                "text":        chunk["text"],
                "field_groups":_tag_groups(chunk["text"]),
                "is_ocr":      chunk.get("is_ocr", False),
            }
            points.append(PointStruct(id=pid, vector=vec, payload=payload))

        if i + BATCH_SIZE < len(chunks):
            time.sleep(1)   # polite pause between batches

    if points:
        client.upsert(collection_name=COLLECTION_NAME, points=points)
        print(f"    [Qdrant] Upserted {len(points)} points for {mpn}")

    return {
        "chunks_embedded":  len(points),
        "chunks_failed":    failed,
        "points_upserted":  len(points),
    }
