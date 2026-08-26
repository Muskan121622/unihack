import os
from dotenv import load_dotenv
load_dotenv()

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

QDRANT_PATH = "qdrant_db_v2_pipeline"
COLLECTION = "evidence_v2"

local_client = QdrantClient(path=QDRANT_PATH)

cloud_url = os.getenv("QDRANT_URL")
cloud_key = os.getenv("QDRANT_API_KEY")
cloud_client = QdrantClient(url=cloud_url, api_key=cloud_key)

try:
    cloud_client.delete_collection(COLLECTION)
except Exception:
    pass
    
cloud_client.create_collection(
    collection_name=COLLECTION,
    vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
)

offset = None
total_migrated = 0

while True:
    records, next_offset = local_client.scroll(
        collection_name=COLLECTION,
        limit=500,
        offset=offset,
        with_payload=True,
        with_vectors=True
    )
    
    if not records:
        break
        
    points = []
    for r in records:
        points.append(PointStruct(
            id=r.id,
            vector=r.vector,
            payload=r.payload
        ))
        
    cloud_client.upsert(
        collection_name=COLLECTION,
        points=points
    )
    
    total_migrated += len(points)
    print(f"Migrated {total_migrated} vectors to the cloud...")
    
    offset = next_offset
    if offset is None:
        break

print(f"Success! Migrated {total_migrated} vectors perfectly to Qdrant Cloud.")
