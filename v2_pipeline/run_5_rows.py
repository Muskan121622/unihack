"""
run_5_rows.py — v2 Pipeline Master Runner
==========================================
Runs all 8 stages for the first 5 products that have a debug.json entry.

Usage (from project root):
    set VOYAGE_API_KEY=your_key
    set GROQ_API_KEY=your_key
    python v2_pipeline/run_5_rows.py

Outputs:
    final_delivery_5_rows.csv      — 252-column delivery CSV
    evidence_debug_5_rows.json     — per-product pipeline diagnostics
    artifacts/{MPN}/v2_*.json      — intermediate files per product
"""

from __future__ import annotations
import os, sys, csv, json, time, traceback

# ── make sure v2_pipeline is importable from the project root ──────────────
_HERE  = os.path.dirname(os.path.abspath(__file__))
_ROOT  = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance

from v2_pipeline import resource_classifier as s1
from v2_pipeline import html_parser         as s2
from v2_pipeline import pdf_processor       as s3
from v2_pipeline import evidence_builder    as s4
from v2_pipeline import qdrant_indexer      as s5
from v2_pipeline import retriever           as s6
from v2_pipeline import llm_extractor       as s7
from v2_pipeline import csv_writer          as s8

# ── Paths ──────────────────────────────────────────────────────────────────
INPUT_CSV    = os.path.join(_ROOT, "Unihack_ Sample Dataset - Input.csv")
TEMPLATE_CSV = os.path.join(_ROOT, "Unihack_ Expected Output - Delivery Format.csv")
DEBUG_JSON   = os.path.join(_ROOT, "discovery_debug.json")
ARTIFACTS    = os.path.join(_ROOT, "artifacts")
QDRANT_PATH  = os.path.join(_ROOT, "qdrant_db_v2_pipeline")
OUT_CSV      = os.path.join(_ROOT, "final_delivery_5_rows.csv")
OUT_DEBUG    = os.path.join(_ROOT, "evidence_debug_5_rows.json")
MAX_ROWS     = 5
COLLECTION   = s5.COLLECTION_NAME


def banner(msg: str):
    print(f"\n{'='*62}\n  {msg}\n{'='*62}")


def _load_debug() -> dict:
    with open(DEBUG_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {e.get("original_mpn", e.get("mpn", "")): e for e in data}


def _alt_mpns(entry: dict) -> list[str]:
    seen, out = set(), []
    for k in ("original_mpn", "mpn"):
        v = entry.get(k, "")
        if v and v not in seen:
            out.append(v); seen.add(v)
    return out


def main():
    banner("v2 Pipeline — 5-Row Test")

    # ── Pre-flight ──────────────────────────────────────────────────────
    missing = [k for k in ("VOYAGE_API_KEY", "GROQ_API_KEY") if not os.getenv(k)]
    for k in missing:
        print(f"[!] WARNING: {k} not set — that stage will be skipped")

    # ── Load template headers ───────────────────────────────────────────
    with open(TEMPLATE_CSV, "r", encoding="utf-8") as f:
        headers = next(csv.reader(f))
    print(f"[*] Schema: {len(headers)} columns")

    # ── Load debug index ────────────────────────────────────────────────
    debug_index = _load_debug()
    print(f"[*] Debug products: {len(debug_index)}")

    # ── Select up to 5 input rows that have debug data ──────────────────
    rows_to_run: list[dict] = []
    with open(INPUT_CSV, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            mpn = row.get("Mfg_Part_Num", "").strip()
            if mpn in debug_index:
                rows_to_run.append(row)
            if len(rows_to_run) >= MAX_ROWS:
                break
    print(f"[*] Running: {[r['Mfg_Part_Num'] for r in rows_to_run]}")

    # ── Qdrant: fresh collection for this run ───────────────────────────
    os.makedirs(QDRANT_PATH, exist_ok=True)
    client = QdrantClient(path=QDRANT_PATH)
    try:
        client.delete_collection(COLLECTION)
        print(f"[*] Dropped old collection: {COLLECTION}")
    except Exception:
        pass
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=s5.VECTOR_DIM, distance=Distance.COSINE),
    )
    print(f"[*] Fresh collection: {COLLECTION}")

    # ── Per-product loop ────────────────────────────────────────────────
    csv_rows: list[dict]  = []
    debug_records: list   = []

    for idx, input_row in enumerate(rows_to_run):
        mpn   = input_row.get("Mfg_Part_Num", "").strip()
        brand = (input_row.get("E1_Brand") or input_row.get("DIB_Brand") or "").strip()
        desc  = input_row.get("Part_Desc", "").strip()

        banner(f"[{idx+1}/{len(rows_to_run)}] {mpn}")

        entry = debug_index.get(mpn, {})
        alt_mpns = _alt_mpns(entry)
        # Use brand from debug if better
        if entry.get("resolved_brand"):
            brand = entry["resolved_brand"]

        art_dir = os.path.join(ARTIFACTS, mpn)
        os.makedirs(art_dir, exist_ok=True)
        final_csv_path = os.path.join(art_dir, "v2_final_output.csv")
        
        # Resume logic: if already done, load it and skip
        if os.path.exists(final_csv_path):
            print(f"[*] Found existing output for {mpn}, skipping pipeline stages...")
            with open(final_csv_path, "r", encoding="utf-8-sig") as f:
                r = list(csv.DictReader(f))
                if r:
                    csv_rows.append(r[0])
                    # Create a dummy debug record so it doesn't crash summary
                    debug_records.append({
                        "mpn": mpn, "discovery": {"mfr_url": ""},
                        "field_coverage": {"fields_filled": sum(1 for v in r[0].values() if str(v).strip()), "fields_empty": 0},
                        "embedding": {"chunks_embedded": 0}, "qdrant_retrieval": {"unique_chunks": 0}
                    })
            continue

        # ---- Stage 1: Resource Classifier --------------------------------
        print(f"\n[S1] Classifying URLs…")
        classified = s1.classify(entry)
        json.dump(classified, open(os.path.join(art_dir,"v2_classified.json"),"w",encoding="utf-8"), indent=2)
        print(f"  MFR URL: {classified['mfr_url']}")
        print(f"  Ref URLs: {len(classified['ref_urls'])}")
        print(f"  PDFs: { {k:len(v) for k,v in classified['pdf_urls'].items() if v} }")
        print(f"  Discarded: {len(classified['discard_urls'])}")

        # ---- Stage 2: HTML Parser ----------------------------------------
        print(f"\n[S2] Parsing HTML pages…")
        html_evs = s2.parse_all_pages(
            ref_urls=classified["ref_urls"],
            mpn=mpn, brand=brand,
            mfr_url=classified["mfr_url"],
            max_pages=5,
        )
        json.dump(html_evs, open(os.path.join(art_dir,"v2_html_evidence.json"),"w",encoding="utf-8"), indent=2, ensure_ascii=False)

        # ---- Stage 3: PDF Processor --------------------------------------
        pdf_results: list = []
        total_pdfs = sum(len(v) for v in classified["pdf_urls"].values())
        if total_pdfs:
            print(f"\n[S3] Processing {total_pdfs} PDF(s)…")
            pdf_results = s3.process_all_pdfs(classified["pdf_urls"], alt_mpns)
            json.dump(pdf_results, open(os.path.join(art_dir,"v2_pdf_evidence.json"),"w",encoding="utf-8"), indent=2, ensure_ascii=False)
        else:
            print(f"\n[S3] No PDFs — skip")

        # ---- Stage 4: Evidence Builder -----------------------------------
        print(f"\n[S4] Building evidence…")
        ev, chunks, ev_stats = s4.build_evidence(
            mpn=mpn, brand=brand,
            classified=classified,
            html_evidences=html_evs,
            pdf_results=pdf_results,
            alternate_mpns=alt_mpns,
        )
        s4.save_evidence(mpn, ARTIFACTS, ev, chunks, ev_stats)

        # ---- Stage 5: Embed + Qdrant ------------------------------------
        embed_stats = {"chunks_embedded":0,"chunks_failed":0,"points_upserted":0}
        if chunks and os.getenv("VOYAGE_API_KEY"):
            print(f"\n[S5] Embedding {len(chunks)} chunks…")
            embed_stats = s5.embed_and_index(client, mpn, brand, chunks)
        elif not os.getenv("VOYAGE_API_KEY"):
            print(f"\n[S5] VOYAGE_API_KEY not set — skip embedding")
        else:
            print(f"\n[S5] No chunks to embed")

        # ---- Stage 6: Retrieval ------------------------------------------
        ret_stats = {"groups_queried":0,"chunks_per_group":{},"total_unique_chunks":0,"query_embed_failures":0}
        retrieved: list = []
        if os.getenv("VOYAGE_API_KEY"):
            print(f"\n[S6] Schema-group retrieval…")
            retrieved, ret_stats = s6.retrieve_for_product(client, mpn, brand)
            json.dump(retrieved, open(os.path.join(art_dir,"v2_retrieved.json"),"w",encoding="utf-8"), indent=2, ensure_ascii=False)
        else:
            print(f"\n[S6] VOYAGE_API_KEY not set — skip retrieval")

        # ---- Stage 7: LLM Extraction ------------------------------------
        llm_out, llm_stats = {}, {"model_used":None,"prompt_chars":0,"success":False,"error":"GROQ_API_KEY not set"}
        if os.getenv("GROQ_API_KEY"):
            print(f"\n[S7] LLM extraction…")
            llm_out, llm_stats = s7.extract_schema(mpn, brand, desc, ev, retrieved)
            json.dump(llm_out, open(os.path.join(art_dir,"v2_llm_output.json"),"w",encoding="utf-8"), indent=2, ensure_ascii=False)
        else:
            print(f"\n[S7] GROQ_API_KEY not set — skip LLM")

        # Pre-fill known facts (runs even without LLM)
        llm_out = s7.pre_fill(llm_out, ev)

        # ---- Stage 8: CSV Row -------------------------------------------
        print(f"\n[S8] Building CSV row…")
        csv_row  = s8.build_csv_row(headers, input_row, ev, llm_out)
        coverage = s8.count_coverage(csv_row, headers)
        csv_rows.append(csv_row)

        # Save individual product CSV
        s8.write_csv(os.path.join(art_dir,"v2_final_output.csv"), headers, [csv_row])

        # Debug record
        debug_records.append(s8.build_debug_record(
            mpn, classified, ev_stats, embed_stats, ret_stats, llm_stats, coverage
        ))

        pct = coverage["fields_filled"] * 100 // len(headers)
        print(
            f"\n[OK] {mpn} -- "
            f"{coverage['fields_filled']}/{len(headers)} fields filled ({pct}%) | "
            f"chunks={embed_stats['chunks_embedded']} | "
            f"qdrant={ret_stats['total_unique_chunks']}"
        )
        if idx < len(rows_to_run) - 1:
            time.sleep(1)

    # ── Write combined outputs ──────────────────────────────────────────
    banner("Writing Outputs")
    s8.write_csv(OUT_CSV, headers, csv_rows)
    s8.write_debug(OUT_DEBUG, debug_records)

    # ── Summary table ───────────────────────────────────────────────────
    banner("Summary")
    print(f"{'MPN':<28} {'Filled':>6} {'Empty':>6} {'Chunks':>7} {'Qdrant':>7} {'MFR':>5}")
    print("-" * 62)
    for rec in debug_records:
        m   = rec["mpn"]
        f   = rec["field_coverage"]["fields_filled"]
        e   = rec["field_coverage"]["fields_empty"]
        ch  = rec["embedding"]["chunks_embedded"]
        q   = rec["qdrant_retrieval"]["unique_chunks"]
        mfr = "OK" if rec["discovery"]["mfr_url"] else "NO"
        print(f"{m:<28} {f:>6} {e:>6} {ch:>7} {q:>7} {mfr:>5}")

    print(f"\n-> CSV  : {OUT_CSV}")
    print(f"-> Debug: {OUT_DEBUG}")
    print("\nNext: inspect evidence_debug_5_rows.json → then fix before scaling to 1,000.")


if __name__ == "__main__":
    main()
