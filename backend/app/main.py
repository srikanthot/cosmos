"""FastAPI application entry point for the PSEG Tech Manual Agent."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config.settings import ALLOWED_ORIGINS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down shared resources (Cosmos DB client)."""
    from app.storage.cosmos_client import close_cosmos, init_cosmos

    logger.info("Starting up — initializing Cosmos DB storage...")
    await init_cosmos()
    logger.info("Startup complete.")
    yield
    logger.info("Shutting down — closing Cosmos DB client...")
    await close_cosmos()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="PSEG Tech Manual Agent",
    description=(
        "Agent-pattern RAG chatbot for GCC High. "
        "Hybrid Azure AI Search + Azure OpenAI, streamed SSE with structured citations. "
        "Persistent chat history via Azure Cosmos DB."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — configurable via ALLOWED_ORIGINS env var (comma-separated).
# Defaults to "*" so local dev and Azure dev deployments work without configuration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
async def health() -> dict:
    """Simple health-check endpoint — includes storage status."""
    from app.storage.cosmos_client import is_storage_enabled

    return {
        "status": "ok",
        "storage": "cosmos" if is_storage_enabled() else "in-memory",
    }
