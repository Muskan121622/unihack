import os

filepath = 'v2_pipeline/run_1000_rows.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

bad_block = '''    os.makedirs(QDRANT_PATH, exist_ok=True)
    client = QdrantClient(path=QDRANT_PATH)'''

good_block = '''    os.makedirs(QDRANT_PATH, exist_ok=True)
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_key = os.getenv("QDRANT_API_KEY")
    if qdrant_url and qdrant_key:
        client = QdrantClient(url=qdrant_url, api_key=qdrant_key)
        print("[*] Connected to QDRANT CLOUD!")
    else:
        client = QdrantClient(path=QDRANT_PATH)
        print("[*] Connected to LOCAL Qdrant")'''

if bad_block in content:
    content = content.replace(bad_block, good_block)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched run_1000_rows.py")
else:
    print("Could not find the block in run_1000_rows.py")
