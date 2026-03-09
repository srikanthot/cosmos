# PSEG Tech Manual Agent

A streaming RAG chatbot for PSEG field technicians. Ask questions against internal technical manuals and get grounded, citation-backed answers in real time.

**Stack:** FastAPI · Azure AI Search (hybrid + vector) · Azure OpenAI (GCC High) · Microsoft Agent Framework SDK · Streamlit

---

## Microsoft Agent Framework SDK

This repo uses the **Microsoft Agent Framework SDK** (`agent-framework-core==1.0.0rc3`) for all LLM orchestration:

| SDK primitive | Role in this repo |
|---|---|
| `AzureOpenAIChatClient` | Azure OpenAI connection (API-key auth, GCC High endpoint) |
| `client.as_agent()` | Creates the `PSEGTechManualAgent` ChatAgent |
| `RagContextProvider(BaseContextProvider)` | Injects retrieved Azure AI Search chunks via `before_run()` |
| `InMemoryHistoryProvider` | Multi-turn conversation memory (local; swap for Cosmos later) |
| `agent.run(stream=True)` → `ResponseStream` | Streams tokens to the SSE pipeline |

---

## Why not Azure AI Foundry Managed Agents?

Azure AI Foundry Managed Agents (and Azure AI Agent Service) are **not available in Azure Government (GCC High)**. This repo implements the same architectural pattern using the Microsoft Agent Framework SDK directly — `AzureOpenAIChatClient` + `ChatAgent` + `ContextProvider` + `InMemoryHistoryProvider` — without requiring the managed service.

---

## How it works

The FastAPI route is intentionally thin. It validates the request, creates a session, and hands off to `AgentRuntime.run_stream()`. All orchestration lives in `agent_runtime/`.

```
POST /chat/stream
        ↓
    routes.py              thin: validate → create session → call runtime
        ↓
  AgentRuntime             owns orchestration
    1. RetrievalTool       embed query → hybrid search Azure AI Search
    2. GATE                abort early if evidence count or avg score too low
    3. rag_provider        store results in session.state (no double Search call)
    4. af_agent.run()      Agent Framework ChatAgent (AzureOpenAIChatClient)
         • InMemoryHistoryProvider.before_run()   load conversation history
         • RagContextProvider.before_run()        inject chunks as instructions
         • LLM streams tokens via ResponseStream
    5. SSE stream          yield tokens + keepalive pings
    6. CitationProvider    dedup + emit structured citations event
        ↓
  SSE stream → Streamlit UI
```

**Hybrid search:** The index has no built-in vectorizer, so `aoai_embeddings.embed()` generates query vectors in the API. Each search call sends both a keyword query and a `VectorizedQuery` against the index's vector field.

**Confidence gate:** If retrieval returns fewer than `MIN_RESULTS` chunks, or the average score is below the gate threshold, the agent short-circuits with a clarifying question instead of hallucinating. When semantic reranker is active, the gate uses `MIN_RERANKER_SCORE` (0–4 scale). When not active, it uses `MIN_AVG_SCORE` (RRF 0.01–0.033 scale).

**Diversity filter:** At most `MAX_CHUNKS_PER_SOURCE` chunks per source file are kept, so the answer doesn't over-index on one document.

**Keepalive pings:** The backend emits `event: ping / data: keepalive` every ~20 seconds during long answers to prevent proxy/browser SSE timeouts.

---

## Azure AI Search Setup

If you are setting up the index from scratch (new Azure subscription), see
**[AZURE_SEARCH_SETUP.md](AZURE_SEARCH_SETUP.md)** for the complete JSON definitions
for the data source, index schema, skillset (OCR + text split + Ada-002 embeddings),
and indexer.

---

## Setup (Windows Git Bash)

### 1. Clone and enter the repo

```bash
cd pseg-agent-pattern-python
```

### 2. Backend environment

```bash
python -m venv .venv-backend
source .venv-backend/Scripts/activate
pip install -r backend/requirements.txt
```

### 3. Frontend environment

Open a second terminal:

```bash
python -m venv .venv-frontend
source .venv-frontend/Scripts/activate
pip install -r frontend/requirements.txt
```

### 4. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in your values. Key things to note:

- `SEARCH_*_FIELD` variables must match your actual Azure AI Search index field names exactly.
- `SEARCH_PAGE_FIELD` should be left blank if your index has no page number field — an empty value means it is skipped in the select list and no page info is shown in citations.
- `SEARCH_SECTION1/2/3_FIELD` map to the three header fields in the layout-based index (`header_1`, `header_2`, `header_3`). Leave blank if unused.
- `AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT` must point to an embeddings model deployment (e.g. `text-embedding-ada-002`). This generates query vectors at search time.
- `USE_SEMANTIC_RERANKER=true` and `SEMANTIC_CONFIG_NAME=manual-semantic-config` enable the semantic reranker. The gate will use `MIN_RERANKER_SCORE` (0–4 scale) instead of `MIN_AVG_SCORE` (RRF 0.01–0.033 scale) when the reranker is active.

### 5. Run

```bash
# Terminal 1 — backend
cd backend
uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
streamlit run frontend/app.py --server.port 8501
```

Open [http://localhost:8501](http://localhost:8501).

---

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | yes | `https://your-resource.openai.azure.us/` |
| `AZURE_OPENAI_API_KEY` | yes | |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | yes | e.g. `gpt-4o-mini` (legacy name) |
| `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME` | yes | Same value — read by Agent Framework SDK |
| `AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT` | yes | e.g. `text-embedding-ada-002` |
| `AZURE_OPENAI_API_VERSION` | no | Default: `2024-06-01` |
| `AZURE_SEARCH_ENDPOINT` | yes | `https://your-search.search.azure.us` |
| `AZURE_SEARCH_API_KEY` | yes | |
| `AZURE_SEARCH_INDEX` | yes | Default: `rag-psegtechm-index-finalv2` |
| `SEARCH_CONTENT_FIELD` | no | Default: `chunk` — main text field sent to LLM |
| `SEARCH_SEMANTIC_CONTENT_FIELD` | no | Default: `chunk_for_semantic` — used by semantic reranker prioritization |
| `SEARCH_VECTOR_FIELD` | no | Default: `text_vector` — 1536-dim Ada-002 vector field |
| `SEARCH_FILENAME_FIELD` | no | Default: `source_file` |
| `SEARCH_URL_FIELD` | no | Default: `source_url` |
| `SEARCH_CHUNK_ID_FIELD` | no | Default: `chunk_id` |
| `SEARCH_TITLE_FIELD` | no | Default: `title` |
| `SEARCH_SECTION1_FIELD` | no | Default: `header_1` — top-level section heading |
| `SEARCH_SECTION2_FIELD` | no | Default: `header_2` — sub-section heading |
| `SEARCH_SECTION3_FIELD` | no | Default: `header_3` — sub-sub-section heading |
| `SEARCH_PAGE_FIELD` | no | Default: `` (empty) — leave blank if index has no page number field |
| `TOP_K` | no | Default: `5`. Max chunks returned after diversity filter |
| `RETRIEVAL_CANDIDATES` | no | Default: `15`. Raw candidates fetched before diversity filter |
| `VECTOR_K` | no | Default: `50`. Nearest-neighbor count for vector query |
| `USE_SEMANTIC_RERANKER` | no | Default: `true`. Requires a semantic configuration in the index |
| `SEMANTIC_CONFIG_NAME` | no | Default: `manual-semantic-config`. Name of the semantic config in your index |
| `QUERY_LANGUAGE` | no | Default: `en-us`. Language hint for semantic reranker |
| `MIN_RESULTS` | no | Default: `2`. Confidence gate — min chunks required to answer |
| `MIN_AVG_SCORE` | no | Default: `0.02`. Gate threshold for base RRF scores (range 0.01–0.033); used when reranker is off |
| `MIN_RERANKER_SCORE` | no | Default: `0.3`. Gate threshold for semantic reranker scores (range 0–4); used when `USE_SEMANTIC_RERANKER=true` |
| `DIVERSITY_BY_SOURCE` | no | Default: `true`. Caps chunks per source file |
| `MAX_CHUNKS_PER_SOURCE` | no | Default: `2`. Max chunks from any single source |
| `DOMINANT_SOURCE_SCORE_RATIO` | no | Default: `1.5`. A source is "dominant" when its top effective score ≥ this × the next source's top score |
| `MAX_CHUNKS_DOMINANT_SOURCE` | no | Default: `4`. Max chunks allowed from the dominant source |
| `SCORE_GAP_MIN_RATIO` | no | Default: `0.55`. Discard chunks whose effective score falls below this fraction of the top score |
| `TRACE_MODE` | no | Default: `true`. Logs ranked chunks with source, section, reranker score, heading, and content preview |
| `BACKEND_URL` | no | Default: `http://localhost:8000`. Frontend uses this to reach the API |

## Project layout

```
pseg-agent-pattern-python/
├── .gitignore
├── .env.example
├── README.md
├── AZURE_SEARCH_SETUP.md              # Index / skillset / indexer JSON for Azure setup
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── __init__.py
│       ├── main.py                        # FastAPI app, CORS, /health
│       ├── config/
│       │   ├── __init__.py
│       │   └── settings.py               # All env vars via python-dotenv
│       ├── api/
│       │   ├── __init__.py
│       │   ├── routes.py                  # POST /chat/stream (thin)
│       │   └── schemas.py                 # ChatRequest, Citation, CitationsPayload
│       ├── agent_runtime/
│       │   ├── __init__.py
│       │   ├── agent.py                   # AgentRuntime — orchestrator
│       │   ├── session.py                 # AgentSession — per-request state
│       │   ├── af_rag_context_provider.py # Agent Framework RAG ContextProvider
│       │   ├── context_providers.py       # Evidence block formatter
│       │   ├── citation_provider.py       # Citation dedup + structuring
│       │   └── prompts.py                 # System prompt templates
│       ├── tools/
│       │   ├── __init__.py
│       │   └── retrieval_tool.py          # Hybrid search + adaptive diversity + TOC filter
│       └── llm/
│           ├── __init__.py
│           ├── af_agent_factory.py        # Agent Framework singleton (AzureOpenAIChatClient)
│           └── aoai_embeddings.py         # Query embedding generation
└── frontend/
    ├── requirements.txt
    └── app.py                             # Streamlit UI + SSE consumer
```
