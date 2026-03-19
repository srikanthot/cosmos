"""Cosmos DB async client — single instance shared for the app lifetime.

Auth modes (controlled by COSMOS_AUTH_MODE env var):
  "key"              → COSMOS_ENDPOINT + COSMOS_KEY  (local / dev)
  "managed_identity" → COSMOS_ENDPOINT + DefaultAzureCredential (production)

Call init_cosmos() once at app startup (FastAPI lifespan).
Call close_cosmos() at app shutdown.
Use get_conversations_container() / get_messages_container() everywhere else.

If COSMOS_ENDPOINT is not configured the module silently disables storage so
the app degrades gracefully to in-memory-only mode.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level singletons — populated by init_cosmos()
_client = None
_conversations_container = None
_messages_container = None


async def init_cosmos() -> None:
    """Initialize Cosmos DB client and containers. Call once at app startup."""
    global _client, _conversations_container, _messages_container

    from app.config.settings import (
        COSMOS_AUTH_MODE,
        COSMOS_AUTO_CREATE_CONTAINERS,
        COSMOS_CONVERSATIONS_CONTAINER,
        COSMOS_DATABASE,
        COSMOS_ENABLE_TTL,
        COSMOS_ENDPOINT,
        COSMOS_KEY,
        COSMOS_MESSAGES_CONTAINER,
        COSMOS_TTL_SECONDS,
    )

    if not COSMOS_ENDPOINT:
        logger.warning(
            "COSMOS_ENDPOINT not configured — persistent chat storage disabled. "
            "Set COSMOS_ENDPOINT (and COSMOS_KEY or use managed identity) to enable."
        )
        return

    try:
        if COSMOS_AUTH_MODE == "managed_identity":
            from azure.cosmos.aio import CosmosClient
            from azure.identity.aio import DefaultAzureCredential

            credential = DefaultAzureCredential()
            _client = CosmosClient(COSMOS_ENDPOINT, credential=credential)
            logger.info(
                "Cosmos DB: initialized with DefaultAzureCredential | endpoint=%s",
                COSMOS_ENDPOINT,
            )
        else:
            from azure.cosmos.aio import CosmosClient

            if not COSMOS_KEY:
                logger.error(
                    "COSMOS_AUTH_MODE=key but COSMOS_KEY is not set — storage disabled"
                )
                return
            _client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
            logger.info(
                "Cosmos DB: initialized with key auth | endpoint=%s", COSMOS_ENDPOINT
            )

        ttl = COSMOS_TTL_SECONDS if COSMOS_ENABLE_TTL else None

        if COSMOS_AUTO_CREATE_CONTAINERS:
            from azure.cosmos import PartitionKey

            db = await _client.create_database_if_not_exists(id=COSMOS_DATABASE)
            logger.info("Cosmos DB: database ready — %s", COSMOS_DATABASE)

            _conversations_container = await db.create_container_if_not_exists(
                id=COSMOS_CONVERSATIONS_CONTAINER,
                partition_key=PartitionKey(path="/user_id"),
                default_ttl=ttl,
            )
            _messages_container = await db.create_container_if_not_exists(
                id=COSMOS_MESSAGES_CONTAINER,
                partition_key=PartitionKey(path="/thread_id"),
                default_ttl=ttl,
            )
        else:
            db = _client.get_database_client(COSMOS_DATABASE)
            _conversations_container = db.get_container_client(
                COSMOS_CONVERSATIONS_CONTAINER
            )
            _messages_container = db.get_container_client(COSMOS_MESSAGES_CONTAINER)

        logger.info(
            "Cosmos DB: containers ready — conversations=%s  messages=%s",
            COSMOS_CONVERSATIONS_CONTAINER,
            COSMOS_MESSAGES_CONTAINER,
        )

    except Exception:
        logger.exception(
            "Cosmos DB initialization failed — storage disabled. "
            "Check COSMOS_ENDPOINT, COSMOS_KEY, and network connectivity."
        )
        _client = None
        _conversations_container = None
        _messages_container = None


async def close_cosmos() -> None:
    """Close the Cosmos DB client. Call at app shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("Cosmos DB: client closed")


def get_conversations_container():
    """Return the conversations ContainerProxy, or None if storage is disabled."""
    return _conversations_container


def get_messages_container():
    """Return the messages ContainerProxy, or None if storage is disabled."""
    return _messages_container


def is_storage_enabled() -> bool:
    """Return True only when both containers are ready."""
    return _conversations_container is not None and _messages_container is not None
