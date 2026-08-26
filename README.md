<div align="center">
  <h1>⚡ Synematic AI: Autonomous Data Extraction & RAG Hub</h1>
  <p><b>An End-to-End AI Pipeline for Automated Web Scraping, Neural Extraction, and Semantic Search</b></p>
</div>

---

## 📖 Overview
**Synematic AI** is an advanced, fully autonomous data engineering and Retrieval-Augmented Generation (RAG) pipeline. Designed to eliminate manual data entry, this system ingests raw product names/SKUs, autonomously scours the internet for technical spec sheets and manuals, embeds the data into a Vector Database, and utilizes Large Language Models (LLMs) to accurately extract over **250+ structured data points** per product. 

This project demonstrates strong proficiency in **Data Engineering, Generative AI (RAG architecture), Web Scraping, and Full-Stack Development.**

---

## 🚀 Key Features

* **🕸️ Autonomous Web Scraping (Stage 0):** Uses **Tavily AI** to intelligently search for official manufacturer documentation, TDS (Technical Data Sheets), and PDFs while filtering out marketplace noise (Amazon, eBay).
* **🧠 Neural Pipeline (Stages 1-8):** Automatically downloads PDFs, parses raw HTML, and chunks documents into context-aware segments.
* **🔍 Semantic Search & Vector DB:** Leverages **Voyage AI** for high-dimensional text embeddings and **Qdrant** for lightning-fast vector similarity search.
* **🤖 LLM-Powered Extraction:** Utilizes **Groq** (Llama-3 70B) to read retrieved context and accurately populate a strict 252-column CSV schema.
* **💻 Interactive RAG Dashboard:** A custom-built **Streamlit** frontend featuring a sleek, dark-mode cyber aesthetic. Features live database monitoring and an interactive chat agent that can answer technical questions about any product in the database.
* **🛡️ Fault-Tolerant & Resumable:** Built-in crash recovery, rate-limit handling (automatic API key rotation), and state-saving ensures the pipeline can process thousands of rows without data loss.

---

## 🛠️ Technology Stack

| Category | Technologies Used |
|----------|------------------|
| **Frontend** | Streamlit, Pandas |
| **Backend** | Python, REST APIs, Asynchronous processing |
| **AI / LLMs** | Groq (Llama-3), OpenAI, Voyage AI (Embeddings) |
| **Vector Database** | Qdrant |
| **Search / Scraping** | Tavily Search API, HTML/PDF Parsing |

---

## 🏗️ Pipeline Architecture

The backend operates on a highly robust 8-stage pipeline:
1. **Resource Classifier:** Categorizes discovered URLs (MFR sites vs. PDFs).
2. **HTML Parser:** Cleans and strips boilerplate from technical webpages.
3. **PDF Processor:** Downloads and OCRs multi-page technical data sheets.
4. **Evidence Builder:** Chunks the extracted text into optimal sizes for embedding.
5. **Qdrant Indexer:** Pushes text embeddings into the local Qdrant Vector DB.
6. **Schema-Driven Retriever:** Queries the Vector DB for specific attribute groups (e.g., Physical specs, Compliance).
7. **LLM Extractor:** Prompts the LLM with the retrieved context to fill the schema.
8. **CSV/Excel Builder:** Formats the output and generates the final deliverable.

---

## ⚙️ Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/synematic-ai.git
   cd synematic-ai
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   # (Includes qdrant-client, streamlit, pandas, requests, etc.)
   ```

3. **Set Environment Variables:**
   Provide your API keys for the AI services. The pipeline supports comma-separated keys for automatic rotation!
   ```powershell
   $env:TAVILY_API_KEY="your_tavily_key"
   $env:VOYAGE_API_KEY="your_voyage_keys"
   $env:GROQ_API_KEY="your_groq_keys"
   ```

---

## 🖥️ Usage

### 1. Run the Backend Extraction Pipeline
Process the dataset, scrape the web, and extract structured data:
```powershell
# Prevent OpenBLAS memory leaks for massive datasets
$env:OPENBLAS_NUM_THREADS="1"
$env:OMP_NUM_THREADS="1"

# Run the 1000-row autonomous pipeline
python v2_pipeline/run_1000_rows.py
```

### 2. Launch the Streamlit Dashboard
Interact with the data and chat with the Semantic RAG Agent:
```powershell
streamlit run app.py
```

---

## 📊 Sample Output — 1,000 Row Extraction Result

> **View the full extracted dataset (252 columns × 1,000 rows) here:**
>
> 👉 **[Final Delivery — 1,000 Rows Google Sheet](https://docs.google.com/spreadsheets/d/1bmTKAHH7A-6qjSBkqPwmjuu_9xCTytzM/edit?usp=sharing&ouid=109796971849085079470&rtpof=true&sd=true)**

This live spreadsheet is the direct output of the autonomous 8-stage pipeline — scraped, embedded, retrieved, and extracted entirely by AI with zero manual effort.

---

## 📈 Impact & Results
* **Scalability:** Successfully processed 1,000+ complex industrial SKUs.
* **Efficiency:** Reduced manual extraction time from hours per product to ~15 seconds per product.
* **Accuracy:** RAG-based grounding ensures zero hallucinations, strictly pulling data from official manufacturer spec sheets.

<div align="center">
  <i>Built with ❤️ for Unihack</i>
</div>
