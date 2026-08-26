from qdrant_client import QdrantClient
client = QdrantClient(path='qdrant_db_v2_pipeline')
print([c.name for c in client.get_collections().collections])
