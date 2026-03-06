# PSEG Tech Manual Agent

A streaming RAG chatbot for PSEG field technicians. Ask questions against internal technical manuals and get grounded, citation-backed answers in real time.

**Stack:** FastAPI · Azure AI Search (hybrid + vector) · Azure OpenAI (GCC High) · Streamlit

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
    3. ContextProvider     format chunks into numbered evidence blocks
    4. Prompts             inject context into grounded system + user prompt
    5. LLM (aoai_chat)     stream answer tokens from Azure OpenAI
    6. CitationProvider    dedup + structure citations from retrieved results
        ↓
  SSE stream → Streamlit UI
```

**Hybrid search:** The index has no built-in vectorizer, so `aoai_embeddings.embed()` generates query vectors in the API. Each search call sends both a keyword query and a `VectorizedQuery` against the index's vector field.

**Confidence gate:** If retrieval returns fewer than `MIN_RESULTS` chunks or the average score is below `MIN_AVG_SCORE`, the agent short-circuits with a clarifying question instead of hallucinating a low-confidence answer.

**Diversity filter:** At most `MAX_CHUNKS_PER_SOURCE` chunks per source file are kept, so the answer doesn't over-index on one document.

**Keepalive pings:** The backend emits `event: ping / data: keepalive` every ~20 seconds during long answers to prevent proxy/browser SSE timeouts.

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

Open `.env` and fill in your values. Two things that are easy to miss:

- `SEARCH_*_FIELD` variables must match your actual Azure AI Search index field names exactly.
- `SEARCH_SECTION_FIELD` should be left blank if your index has no section field — an empty value means that field is skipped in the select list.
- `AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT` must point to an embeddings model deployment (e.g. `text-embedding-ada-002`). This generates query vectors at search time.

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
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | yes | e.g. `gpt-4o-mini` |
| `AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT` | yes | e.g. `text-embedding-ada-002` |
| `AZURE_OPENAI_API_VERSION` | no | Default: `2024-06-01` |
| `AZURE_SEARCH_ENDPOINT` | yes | `https://your-search.search.azure.us` |
| `AZURE_SEARCH_API_KEY` | yes | |
| `AZURE_SEARCH_INDEX` | yes | Name of your existing index |
| `SEARCH_CONTENT_FIELD` | no | Default: `content` |
| `SEARCH_VECTOR_FIELD` | no | Default: `contentVector` |
| `SEARCH_FILENAME_FIELD` | no | Default: `source_file` |
| `SEARCH_PAGE_FIELD` | no | Default: `page_number` |
| `SEARCH_CHUNK_ID_FIELD` | no | Default: `chunk_id` |
| `SEARCH_URL_FIELD` | no | Default: `source_url` |
| `SEARCH_SECTION_FIELD` | no | Leave blank if your index has no section field |
| `TOP_K` | no | Default: `5`. Max chunks returned from search |
| `VECTOR_K` | no | Default: `50`. Nearest-neighbor count for vector query |
| `USE_SEMANTIC_RERANKER` | no | Default: `false`. Set `true` if your index has a semantic configuration |
| `SEMANTIC_CONFIG_NAME` | no | Default: `default`. Name of the semantic config in your index |
| `QUERY_LANGUAGE` | no | Default: `en-us`. Language hint for semantic reranker |
| `MIN_RESULTS` | no | Default: `3`. Confidence gate — min chunks required to answer |
| `MIN_AVG_SCORE` | no | Default: `0.2`. Confidence gate — min average relevance score |
| `DIVERSITY_BY_SOURCE` | no | Default: `true`. Caps chunks per source file |
| `MAX_CHUNKS_PER_SOURCE` | no | Default: `2` |
| `TRACE_MODE` | no | Default: `true`. Logs source / page / chunk_id / score per result |
| `BACKEND_URL` | no | Default: `http://localhost:8000`. Frontend uses this to reach the API |

## Project layout

```
pseg-agent-pattern-python/
├── .gitignore
├── .env.example
├── README.md
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
│       │   ├── context_providers.py       # Evidence block formatter
│       │   ├── citation_provider.py       # Citation dedup + structuring
│       │   └── prompts.py                 # System + user prompt templates
│       ├── tools/
│       │   ├── __init__.py
│       │   └── retrieval_tool.py          # Hybrid search + diversity filter
│       └── llm/
│           ├── __init__.py
│           ├── aoai_chat.py               # Azure OpenAI chat streaming
│           └── aoai_embeddings.py         # Query embedding generation
└── frontend/
    ├── requirements.txt
    └── app.py                             # Streamlit UI + SSE consumer
```
