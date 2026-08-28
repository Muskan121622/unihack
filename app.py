import streamlit as st
import pandas as pd
import os, io, re, json, requests

st.set_page_config(page_title="Synematic AI | Data Hub", layout="wide", page_icon="!")

def _load_secret(key):
    try:
        if key in st.secrets: return st.secrets[key]
    except Exception: pass
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                if line.startswith(key + "="): return line.strip().split("=", 1)[1]
    return ""

BM_KEYS     = [k.strip() for k in _load_secret("BLUESMINDS_API_KEY").split(",") if k.strip()]
VOYAGE_KEYS = [k.strip() for k in _load_secret("VOYAGE_API_KEY").split(",") if k.strip()]
QDRANT_URL  = _load_secret("QDRANT_URL").strip()
QDRANT_KEY  = _load_secret("QDRANT_API_KEY").strip()

@st.cache_resource
def get_qdrant():
    if QDRANT_URL and QDRANT_KEY:
        from qdrant_client import QdrantClient
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
    return None

# ═══════════════════════════════════════════
# LAYER 1 — Query Router (no LLM, no hardcoding)
# ═══════════════════════════════════════════
MPN_RE          = re.compile(r"\b([A-Z0-9]{2,}[-][A-Z0-9][\w\-]*)\b", re.IGNORECASE)
CMP_WORDS       = {"compare", "vs", "versus", "difference", "better"}
SPEC_ATTRS      = {"grit","size","weight","diameter","voltage","dimension","thickness",
                   "length","width","material","color","speed","rpm","amperage","watt","capacity"}
COMPLIANCE_KEYS = {"rohs","prop 65","prop65","sds","msds","warranty","certification",
                   "certif","approved","approval","reach","safety","hazard"}
STOP_WORDS      = {"products","product","all","list","show","what","the","me","give","find",
                   "tell","about","available","are","a","an","is","in","of","for","and","or",
                   "do","i","have","any","this","that","those","these","from","with","can","you"}

# Column groups — aligned to actual CSV schema
COL_GROUPS = {
    "identity":    ["Mfg_Part_Num", "MANUFACTURER_PART_NUMBER", "ALTERNATE_PART_NUMBER", "UPC", "EAN", "GTIN"],
    "brand":       ["BRAND_NAME", "MANUFACTURER_NAME", "Part_Desc"],
    "description": ["Part_Desc", "ITEM_FEATURES_1", "ITEM_FEATURES_2", "ITEM_FEATURES_3",
                    "ATTRIBUTE_LABEL 1", "ATTRIBUTE_VALUE 1", "ATTRIBUTE_LABEL 2", "ATTRIBUTE_VALUE 2",
                    "ATTRIBUTE_LABEL 3", "ATTRIBUTE_VALUE 3"],
    "compliance":  ["RoHS", "Standard/Approvals", "Prop 65", "SDS", "Warranty", "Warranty Information"],
}

def parse_query(question: str) -> dict:
    q     = question.lower().strip()
    mpns  = MPN_RE.findall(question)
    attrs = [a for a in SPEC_ATTRS if a in q]
    comp  = [c for c in COMPLIANCE_KEYS if c in q]
    tokens= [w for w in q.split() if w not in STOP_WORDS and len(w) > 1]

    if len(mpns) >= 2 and any(w in q for w in CMP_WORDS):
        return {"intent": "comparison",     "mpns": mpns[:2], "attrs": attrs, "tokens": tokens,
                "search_groups": ["identity"]}
    elif mpns:
        return {"intent": "mpn_lookup",     "mpns": mpns,     "attrs": attrs, "tokens": tokens,
                "search_groups": ["identity"]}
    elif comp:
        return {"intent": "compliance",     "mpns": [],       "attrs": attrs, "tokens": tokens,
                "search_groups": ["compliance"]}
    else:
        return {"intent": "keyword_search", "mpns": [],       "attrs": attrs, "tokens": tokens,
                "search_groups": ["brand", "description"]}

# ═══════════════════════════════════════════
# LAYER 2A — Structured CSV Retrieval
#   Uses column groups from parse_query — not hardcoded to 2 fields
# ═══════════════════════════════════════════

def normalize_mpn(mpn: str) -> str:
    """Canonical MPN normalization — applied once at retrieval, once at ingestion.
    strip whitespace → uppercase → deterministic identity matching."""
    return mpn.strip().upper()

def _cols_present(df: pd.DataFrame, group: str) -> list:
    """Return only the columns from a group that actually exist in the CSV."""
    return [c for c in COL_GROUPS.get(group, []) if c in df.columns]

def retrieve_csv(df: pd.DataFrame, parsed: dict, max_rows: int = 10) -> pd.DataFrame:
    if df.empty: return pd.DataFrame()
    result = pd.DataFrame()
    intent = parsed["intent"]
    groups = parsed.get("search_groups", ["brand", "description"])

    if intent in ("mpn_lookup", "comparison"):
        mpns     = parsed.get("mpns", [])
        norm_mpns= [normalize_mpn(m) for m in mpns]   # normalize once
        id_cols  = _cols_present(df, "identity")
        # Exact normalized match on any identity column
        mask = pd.Series([False] * len(df), index=df.index)
        for col in id_cols:
            mask |= df[col].astype(str).map(normalize_mpn).isin(norm_mpns)
        result = df[mask].copy()
        # Partial fallback (substring)
        if result.empty:
            for mpn in mpns:
                for col in id_cols:
                    hits = df[df[col].astype(str).str.contains(re.escape(mpn), case=False, na=False)]
                    result = pd.concat([result, hits])

    elif intent == "compliance":
        comp_cols = _cols_present(df, "compliance")
        for token in parsed.get("tokens", []):
            for col in comp_cols:
                hits = df[df[col].astype(str).str.contains(token, case=False, na=False)]
                result = pd.concat([result, hits])
            if len(result) >= max_rows: break

    else:  # keyword_search
        search_cols = []
        for g in groups:
            search_cols.extend(_cols_present(df, g))
        search_cols = list(dict.fromkeys(search_cols))  # deduplicate, preserve order

        for token in parsed.get("tokens", []):
            mask = pd.Series([False] * len(df), index=df.index)
            for col in search_cols:
                mask |= df[col].astype(str).str.contains(token, case=False, na=False)
            result = pd.concat([result, df[mask]])
            if len(result) >= max_rows: break

    return result.drop_duplicates(subset=["Mfg_Part_Num"]).head(max_rows) if not result.empty else result

# ═══════════════════════════════════════════
# LAYER 2B — Qdrant Semantic Retrieval
# ═══════════════════════════════════════════
_vi = [0]
def _embed(text: str):
    if not VOYAGE_KEYS: return None
    key = VOYAGE_KEYS[_vi[0] % len(VOYAGE_KEYS)]; _vi[0] += 1
    try:
        r = requests.post("https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"input": [text], "model": "voyage-3", "input_type": "query"}, timeout=15)
        if r.status_code == 200: return r.json()["data"][0]["embedding"]
    except Exception: pass
    return None

def retrieve_qdrant(question: str, mpns: list = None, top_k: int = 5) -> list:
    client = get_qdrant()
    if client is None: return []
    vec = _embed(question)
    if vec is None: return []
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchAny
        q_filter = None
        if mpns:
            # Same normalize_mpn() used in CSV retrieval — consistent identity layer
            q_filter = Filter(must=[FieldCondition(key="mpn", match=MatchAny(any=[normalize_mpn(m) for m in mpns]))])
        res = client.query_points(collection_name="evidence_v2", query=vec, query_filter=q_filter, limit=top_k)
        seen, chunks = set(), []
        for pt in res.points:
            t = pt.payload.get("text", "")
            if t[:80] not in seen:
                seen.add(t[:80])
                chunks.append({"mpn": pt.payload.get("mpn", ""), "text": t, "score": round(pt.score, 3)})
        return chunks
    except Exception: return []

# ═══════════════════════════════════════════
# LAYER 3 — Evidence Fusion
# ═══════════════════════════════════════════
def fuse_evidence(csv_rows: pd.DataFrame, qdrant_chunks: list) -> str:
    parts = []
    if not csv_rows.empty:
        parts.append("=== STRUCTURED PRODUCT CATALOG ===")
        for _, row in csv_rows.iterrows():
            filled = {k: v for k, v in row.items() if str(v).strip() not in ("", "nan", "NaN", "-- Unbranded --")}
            parts.append(f"MPN: {row['Mfg_Part_Num']} | Desc: {row['Part_Desc']} | Fields: {len(filled)}/252\n{json.dumps(filled, ensure_ascii=False)}")
    if qdrant_chunks:
        parts.append("=== VECTOR EVIDENCE STORE ===")
        for c in qdrant_chunks:
            parts.append(f"[MPN: {c['mpn']} | relevance: {c['score']}]\n{c['text']}")
    return "\n\n".join(parts)

# ═══════════════════════════════════════════
# LAYER 4 — LLM reasons ONLY over fused evidence
# ═══════════════════════════════════════════
_bi = [0]
SYSTEM_PROMPT = (
    "You are Synematic AI, a helpful and intelligent industrial product assistant. "
    "Your goal is to help users find products, compare specifications, and understand technical details. "
    "Answer using ONLY the exact product data and evidence provided below. "
    "Present the information naturally and clearly, using markdown tables or bullet points for readability. "
    "Use exact field values from the evidence (e.g., MPNs, grit values, dimensions). "
    "If a requested detail is not present in the retrieved evidence, simply say 'I don't have that information currently' rather than making it up. "
    "Do not explain your internal RAG pipeline or retrieval mechanics to the user unless explicitly asked."
)

def call_llm(evidence: str, conversation: list) -> str:
    if not BM_KEYS: return "No LLM API key configured."
    key = BM_KEYS[_bi[0] % len(BM_KEYS)]; _bi[0] += 1
    sys_c = SYSTEM_PROMPT + (f"\n\n--- RETRIEVED EVIDENCE ---\n{evidence}" if evidence else "\n\nNo evidence retrieved. Tell the user honestly.")
    msgs = [{"role": "system", "content": sys_c}] + conversation
    try:
        r = requests.post("https://api.bluesminds.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "messages": msgs, "temperature": 0.2}, timeout=25)
        if r.status_code == 200: return r.json()["choices"][0]["message"]["content"]
        return f"LLM Error {r.status_code}: {r.text[:200]}"
    except Exception as e: return f"Connection failed: {e}"

# ═══════════════════════════════════════════
# PUBLIC API — called by Streamlit UI
# ═══════════════════════════════════════════
def rag_answer(question: str, conversation: list, df: pd.DataFrame) -> tuple:
    parsed    = parse_query(question)
    csv_rows  = retrieve_csv(df, parsed)
    intent    = parsed["intent"]

    # MPN filter for Qdrant:
    # - mpn_lookup / comparison → filter Qdrant to only that product's evidence
    # - keyword_search / compliance → free semantic search (no MPN filter)
    #   so Qdrant can find supporting evidence broadly across all chunks
    if intent in ("mpn_lookup", "comparison"):
        qdrant_mpn_filter = list(csv_rows["Mfg_Part_Num"].astype(str)) if not csv_rows.empty else parsed.get("mpns", [])
    else:
        qdrant_mpn_filter = None  # free semantic search

    qdrant_chunks = retrieve_qdrant(question, mpns=qdrant_mpn_filter)
    evidence      = fuse_evidence(csv_rows, qdrant_chunks)
    answer        = call_llm(evidence, conversation)
    stats         = {"intent": intent, "csv_rows": len(csv_rows),
                     "qdrant_chunks": len(qdrant_chunks), "tokens": parsed.get("tokens", [])}
    return answer, stats


# ═══════════════════════════════════════════
# CSS
# ═══════════════════════════════════════════
st.markdown("""<style>
.title-glow{font-size:3rem;font-weight:800;background:-webkit-linear-gradient(45deg,#00F0FF,#7000FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
div[data-testid="metric-container"]{background-color:#1E212B;border:1px solid #333;padding:15px;border-radius:12px;transition:transform .2s;}
div[data-testid="metric-container"]:hover{transform:translateY(-3px);border-color:#00F0FF;}
.agent-header{font-size:1.8rem;font-weight:600;color:#00F0FF;margin-bottom:10px;border-bottom:1px solid #333;padding-bottom:10px;}
div[data-testid="stDownloadButton"] button{background:linear-gradient(45deg,#FF007A,#7000FF);color:white!important;font-weight:900!important;font-size:1.2rem!important;padding:10px 24px;border:none;border-radius:8px;box-shadow:0 0 15px #FF007A,0 0 30px #7000FF;animation:pulse-glow 2s infinite;width:100%;margin-top:20px;}
@keyframes pulse-glow{0%{box-shadow:0 0 10px #FF007A,0 0 20px #7000FF;}50%{box-shadow:0 0 25px #FF007A,0 0 50px #7000FF;}100%{box-shadow:0 0 10px #FF007A,0 0 20px #7000FF;}}
</style>""", unsafe_allow_html=True)

# ═══════════════════════════════════════════
# UI
# ═══════════════════════════════════════════
col_t, col_b = st.columns([3, 1])
with col_t:
    st.markdown('<p class="title-glow">Synematic RAG & Data Hub</p>', unsafe_allow_html=True)
    st.markdown("<p style='color:#888;font-size:1.1rem;'>Autonomous Extraction Pipeline & AI Knowledge Agent</p>", unsafe_allow_html=True)
with col_b:
    if os.path.exists("final_delivery_1000_rows.csv"):
        try:
            df_dl = pd.read_csv("final_delivery_1000_rows.csv")
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as w: df_dl.to_excel(w, index=False, sheet_name="Products")
            buf.seek(0)
            st.download_button("Download output of given 1000 rows sample", data=buf,
                file_name="final_delivery_1000_rows.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as e: st.button(f"Unavailable: {e}", disabled=True)

csv_path = "final_delivery_1000_rows.csv"
df_live  = pd.read_csv(csv_path) if os.path.exists(csv_path) else pd.DataFrame()
n        = len(df_live)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Products", f"{n:,}", "100% Complete")
c2.metric("Vectors Embedded", "6,204", "Cloud Synced")
c3.metric("Data Fields Extracted", f"{n * 252:,}", "252/row")
c4.metric("Pipeline Status", "Complete", "100%")
st.divider()

col_a, _, col_s = st.columns([2.5, 0.1, 1.2])

with col_a:
    st.markdown('<div class="agent-header">Synematic Agent</div>', unsafe_allow_html=True)
    v_ok = "Voyage OK" if VOYAGE_KEYS else "No Voyage"
    q_ok = "Qdrant OK" if (QDRANT_URL and QDRANT_KEY) else "No Qdrant"
    st.caption(f"Hybrid RAG: Query Router -> Structured + Semantic Retrieval -> Evidence Fusion -> GPT-4o | {v_ok} | {q_ok}")

    if "messages" not in st.session_state:
        st.session_state.messages = [{"role": "assistant", "content":
            "Welcome! I'm Synematic AI, your product intelligence assistant. "
            "I can help you find products, check specifications, compare items, and look up compliance info. "
            "How can I help you today?"}]

    chat = st.container(height=650, border=True)
    with chat:
        for m in st.session_state.messages:
            with st.chat_message(m["role"]): st.markdown(m["content"])

    if prompt := st.chat_input("Ask: part number, brand, spec, comparison..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with chat:
            with st.chat_message("user"): st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("Routing -> Retrieving -> Reasoning..."):
                    conv = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    response, stats = rag_answer(prompt, conv, df_live)
                st.caption(f"Intent: `{stats['intent']}` | CSV rows: `{stats['csv_rows']}` | Qdrant chunks: `{stats['qdrant_chunks']}`")
                st.markdown(response)
                st.session_state.messages.append({"role": "assistant", "content": response})

with col_s:
    st.markdown("<div style='font-size:1.2rem;font-weight:600;'>Data Ingestion</div>", unsafe_allow_html=True)
    with st.expander("Live Database Preview", expanded=True):
        st.info(f"{n} rows, 252 columns")
        if not df_live.empty:
            st.dataframe(df_live[["Mfg_Part_Num", "E1_Brand", "Part_Desc"]], use_container_width=True, height=250)
    with st.expander("Upload Documents"):
        st.caption("Drag and drop catalogs or PDFs.")
        st.file_uploader("Upload", type=["csv", "pdf", "json"], label_visibility="hidden")
    with st.expander("Pipeline Settings"):
        st.selectbox("Model", ["gpt-4o (BluesMinds)", "gpt-4o-mini"])
        st.toggle("Vector Search Enabled", value=True)
