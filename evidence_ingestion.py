import os
import json
import time
import requests
import fitz # PyMuPDF
import openpyxl
from bs4 import BeautifulSoup
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

VOYAGE_API_KEY = "pa-kk7t5YaQSkS0JL0ADD2iXN4gO30eTrDv3gtbInl7lxH"

def embed_texts(texts):
    url = "https://api.voyageai.com/v1/embeddings"
    headers = {"Authorization": f"Bearer {VOYAGE_API_KEY}", "Content-Type": "application/json"}
    
    for attempt in range(10):
        try:
            resp = requests.post(url, headers=headers, json={"input": texts, "model": "voyage-3"}, timeout=30)
            if resp.status_code == 200:
                return [d["embedding"] for d in resp.json()["data"]]
            elif resp.status_code == 429:
                print("    [!] Voyage AI Rate Limit (429). Waiting 20 seconds...")
                time.sleep(20)
            else:
                print(f"    [!] Voyage Error {resp.status_code}: {resp.text}")
                time.sleep(2)
        except Exception as e:
            time.sleep(5)
    return None

def extract_pdf_text(filepath):
    text = ""
    try:
        doc = fitz.open(filepath)
        for page in doc:
            t = page.get_text()
            if t: text += t + "\n"
    except Exception as e:
        print(f"Error parsing PDF {filepath}: {e}")
    return text

def chunk_text(text, chunk_size=1000, overlap=200):
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start+chunk_size])
        if chunk_size - overlap <= 0:
            break
        start += chunk_size - overlap
    return chunks

def run_ingestion():
    if not os.path.exists("discovery_debug_3m.json"):
        print("No discovery_debug_3m.json found.")
        return
        
    with open("discovery_debug_3m.json", "r", encoding="utf-8") as f:
        data = json.load(f)
        
    client = QdrantClient(path="qdrant_db_v3")
    try: client.delete_collection("evidence")
    except: pass
    client.create_collection(
        collection_name="evidence",
        vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
    )
    
    local_docs_path = "artifacts/3MABR-7100075678/raw_documents"
    all_chunks = []
    
    if os.path.exists(local_docs_path):
        for fname in os.listdir(local_docs_path):
            fpath = os.path.join(local_docs_path, fname)
            if fname.lower().endswith(".pdf"):
                text = extract_pdf_text(fpath)
                parts = chunk_text(text)
                for i, p in enumerate(parts):
                    all_chunks.append({
                        "mpn": "3MABR-7100075678",
                        "brand": "3M",
                        "source_type": "pdf",
                        "source_url": f"file://{fname}",
                        "section_id": f"pdf_{fname}_{i}",
                        "chunk_id": i,
                        "text": p
                    })
                    
    for item in data:
        mpn = item.get("original_mpn")
        if mpn != "3MABR-7100075678": continue
        
        urls = []
        for q in item.get("queries", []):
            for res in q.get("results", []):
                if res.get("status") == "ACCEPT" or res.get("score", 0) > -30:
                    urls.append(res.get("url"))
        urls = list(dict.fromkeys(urls))
        
        for u in urls:
            if u.lower().endswith(".pdf"):
                try:
                    resp = requests.get(u, timeout=10)
                    if resp.status_code == 200:
                        temp_pdf = f"temp_{mpn}.pdf"
                        with open(temp_pdf, "wb") as f: f.write(resp.content)
                        text = extract_pdf_text(temp_pdf)
                        parts = chunk_text(text)
                        for i, p in enumerate(parts):
                            all_chunks.append({
                                "mpn": mpn,
                                "brand": "3M",
                                "source_type": "pdf",
                                "source_url": u,
                                "section_id": f"url_pdf_{i}",
                                "chunk_id": i,
                                "text": p
                            })
                        os.remove(temp_pdf)
                except Exception as e:
                    pass
            else:
                try:
                    resp = requests.get(u, timeout=5)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.content, "html.parser")
                        text = soup.get_text(separator="\n", strip=True)
                        parts = chunk_text(text)
                        for i, p in enumerate(parts):
                            all_chunks.append({
                                "mpn": mpn,
                                "brand": "3M",
                                "source_type": "html",
                                "source_url": u,
                                "section_id": f"html_{i}",
                                "chunk_id": i,
                                "text": p
                            })
                except:
                    pass
                    
    print(f"[*] Total chunks to embed: {len(all_chunks)}")
    
    # Batch embed
    points = []
    batch_size = 72 # max is 128 but to stay under TPM limit let's use 72
    
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i:i+batch_size]
        print(f"    Embedding batch {i} to {i+len(batch)} of {len(all_chunks)}...")
        texts = [c["text"] for c in batch]
        
        vecs = embed_texts(texts)
        if vecs:
            for j, vec in enumerate(vecs):
                points.append(PointStruct(id=i+j+1, vector=vec, payload=batch[j]))
                
    if points:
        client.upsert(collection_name="evidence", points=points)
        print(f"[*] Indexed {len(points)} chunks into Qdrant!")

if __name__ == '__main__':
    run_ingestion()
