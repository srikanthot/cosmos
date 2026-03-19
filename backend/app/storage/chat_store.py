"""Chat storage layer — all Cosmos DB operations for conversations and messages.

All public functions are async and return None / [] gracefully when storage is
disabled (no COSMOS_ENDPOINT configured).  Routes and AgentRuntime call these
functions directly — no Cosmos SDK calls are scattered elsewhere.

Container layout:
  conversations  — partitioned by /user_id
  messages       — partitioned by /thread_id

Sequence numbers are derived from the conversation's message_count field, which
is incremented atomically as part of each message append.  This is safe for the
single-writer-per-thread pattern of a conversational chatbot.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from app.storage.cosmos_client import (
    get_conversations_container,
    get_messages_container,
    is_storage_enabled,
)
from app.storage.models import ConversationRecord, MessageRecord

logger = logging.getLogger(__name__)

_PREVIEW_MAX_CHARS = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview(text: str) -> str:
    """Truncate to PREVIEW_MAX_CHARS at a word boundary."""
    text = text.strip()
    if len(text) <= _PREVIEW_MAX_CHARS:
        return text
    truncated = text[:_PREVIEW_MAX_CHARS].rsplit(" ", 1)[0]
    return truncated + "…"


def generate_title(question: str) -> str:
    """Generate a readable conversation title from the first user message.

    Strips common question prefixes, capitalizes, and truncates.
    No LLM call is made.

    Examples:
      "what are the steps for maintaining the 22.5 kVA transformer?"
        → "Steps for maintaining the 22.5 kVA transformer"
      "How do I reset the breaker?"
        → "Reset the breaker"
    """
    q = question.strip().rstrip("?.!")

    # Strip common leading question/filler phrases
    filler = re.compile(
        r"^(?:"
        r"what(?:\s+are|\s+is|\s+were|\s+was)?\s+(?:the\s+)?(?:steps?\s+(?:for|to|of)\s+)?"
        r"|how\s+(?:do\s+(?:i|we|you)\s+|to\s+|can\s+(?:i|we)\s+)?"
        r"|can\s+you\s+(?:explain\s+|describe\s+|tell\s+me\s+(?:about\s+)?)?"
        r"|please\s+(?:explain\s+|describe\s+|tell\s+me\s+(?:about\s+)?)?"
        r"|tell\s+me\s+(?:about\s+)?"
        r")",
        re.IGNORECASE,
    )
    q = filler.sub("", q).strip()

    if not q:
        return "New Chat"

    # Capitalize first letter
    q = q[0].upper() + q[1:]

    # Truncate to 70 chars at word boundary
    if len(q) > 70:
        truncated = q[:67].rsplit(" ", 1)[0]
        q = truncated + "…"

    return q


def _doc_to_conversation(doc: dict) -> ConversationRecord:
    return ConversationRecord.model_validate(doc)


def _doc_to_message(doc: dict) -> MessageRecord:
    return MessageRecord.model_validate(doc)


# ---------------------------------------------------------------------------
# Conversation operations
# ---------------------------------------------------------------------------

async def create_conversation(
    thread_id: str,
    user_id: str,
    user_name: str = "",
    title: str = "New Chat",
) -> Optional[ConversationRecord]:
    """Create and persist a new conversation document. Returns None if storage disabled."""
    if not is_storage_enabled():
        return None

    container = get_conversations_container()
    conv = ConversationRecord(
        id=thread_id,
        thread_id=thread_id,
        user_id=user_id,
        user_name=user_name,
        title=title,
    )
    try:
        body = conv.model_dump(mode="json")
        await container.upsert_item(body=body)
        logger.info(
            "chat_store: conversation created | thread=%s user=%s title=%r",
            thread_id, user_id, title,
        )
        return conv
    except Exception:
        logger.exception(
            "chat_store: failed to create conversation | thread=%s user=%s",
            thread_id, user_id,
        )
        return None


async def get_conversation(
    thread_id: str,
    user_id: str,
) -> Optional[ConversationRecord]:
    """Read a conversation by thread_id.  Returns None if not found or storage disabled."""
    if not is_storage_enabled():
        return None

    container = get_conversations_container()
    try:
        doc = await container.read_item(item=thread_id, partition_key=user_id)
        return _doc_to_conversation(doc)
    except Exception as exc:
        # 404 is normal (new thread); log others as warnings
        status = getattr(exc, "status_code", None)
        if status != 404:
            logger.warning(
                "chat_store: failed to read conversation | thread=%s user=%s | %s",
                thread_id, user_id, exc,
            )
        return None


async def get_or_create_conversation(
    thread_id: str,
    user_id: str,
    user_name: str = "",
) -> Optional[ConversationRecord]:
    """Return existing conversation or create a new one."""
    conv = await get_conversation(thread_id, user_id)
    if conv is not None:
        logger.info(
            "chat_store: conversation loaded | thread=%s user=%s turns=%d",
            thread_id, user_id, conv.message_count,
        )
        return conv
    return await create_conversation(thread_id, user_id, user_name)


async def list_conversations(
    user_id: str,
    limit: int = 20,
    include_deleted: bool = False,
) -> list[ConversationRecord]:
    """Return recent conversations for a user, ordered by last_message_at desc."""
    if not is_storage_enabled():
        return []

    container = get_conversations_container()
    deleted_clause = "" if include_deleted else "AND c.is_deleted = false"
    query = (
        f"SELECT * FROM c WHERE c.user_id = @user_id {deleted_clause} "
        f"ORDER BY c.last_message_at DESC OFFSET 0 LIMIT @limit"
    )
    params = [
        {"name": "@user_id", "value": user_id},
        {"name": "@limit", "value": limit},
    ]
    try:
        items = []
        async for doc in container.query_items(
            query=query,
            parameters=params,
        ):
            items.append(_doc_to_conversation(doc))
        logger.info(
            "chat_store: listed %d conversations | user=%s", len(items), user_id
        )
        return items
    except Exception:
        logger.exception(
            "chat_store: failed to list conversations | user=%s", user_id
        )
        return []


async def soft_delete_conversation(thread_id: str, user_id: str) -> bool:
    """Mark a conversation as deleted without removing the document."""
    if not is_storage_enabled():
        return False

    conv = await get_conversation(thread_id, user_id)
    if conv is None:
        return False

    container = get_conversations_container()
    try:
        conv.is_deleted = True
        conv.updated_at = _utcnow()
        await container.upsert_item(body=conv.model_dump(mode="json"))
        logger.info(
            "chat_store: conversation soft-deleted | thread=%s user=%s",
            thread_id, user_id,
        )
        return True
    except Exception:
        logger.exception(
            "chat_store: failed to soft-delete conversation | thread=%s", thread_id
        )
        return False


async def update_conversation_title(
    thread_id: str,
    user_id: str,
    title: str,
) -> bool:
    """Update the title of a conversation."""
    if not is_storage_enabled():
        return False

    conv = await get_conversation(thread_id, user_id)
    if conv is None:
        return False

    container = get_conversations_container()
    try:
        conv.title = title
        conv.updated_at = _utcnow()
        await container.upsert_item(body=conv.model_dump(mode="json"))
        logger.info(
            "chat_store: title updated | thread=%s title=%r", thread_id, title
        )
        return True
    except Exception:
        logger.exception(
            "chat_store: failed to update title | thread=%s", thread_id
        )
        return False


async def _update_conversation_after_message(
    thread_id: str,
    user_id: str,
    role: str,
    content: str,
    first_user_message: bool = False,
) -> None:
    """Update conversation metadata after a message is appended."""
    conv = await get_conversation(thread_id, user_id)
    if conv is None:
        return

    container = get_conversations_container()
    now = _utcnow()
    conv.updated_at = now
    conv.last_message_at = now
    conv.message_count += 1

    preview = _preview(content)
    if role == "user":
        conv.last_user_message_preview = preview
        if first_user_message:
            conv.title = generate_title(content)
    else:
        conv.last_assistant_message_preview = preview

    try:
        await container.upsert_item(body=conv.model_dump(mode="json"))
    except Exception:
        logger.exception(
            "chat_store: failed to update conversation metadata | thread=%s",
            thread_id,
        )


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------

async def append_message(
    thread_id: str,
    user_id: str,
    role: str,
    content: str,
    citations: Optional[list[dict]] = None,
    status: str = "complete",
) -> Optional[MessageRecord]:
    """Append a user or assistant message and update conversation metadata."""
    if not is_storage_enabled():
        return None

    container = get_messages_container()
    conv_container = get_conversations_container()

    # Determine sequence — read current message_count as sequence base
    conv = await get_conversation(thread_id, user_id)
    current_count = conv.message_count if conv else 0
    sequence = current_count + 1

    msg = MessageRecord(
        thread_id=thread_id,
        user_id=user_id,
        role=role,
        content=content,
        citations=citations or [],
        sequence=sequence,
        status=status,
    )

    try:
        await container.upsert_item(body=msg.model_dump(mode="json"))
        logger.info(
            "chat_store: message saved | thread=%s role=%s seq=%d len=%d",
            thread_id, role, sequence, len(content),
        )
    except Exception:
        logger.exception(
            "chat_store: failed to save message | thread=%s role=%s", thread_id, role
        )
        return None

    # Update conversation metadata (non-blocking; failure logged but not raised)
    is_first_user_msg = (role == "user" and current_count == 0)
    await _update_conversation_after_message(
        thread_id, user_id, role, content, first_user_message=is_first_user_msg
    )

    return msg


async def append_user_message(
    thread_id: str, user_id: str, content: str
) -> Optional[MessageRecord]:
    return await append_message(thread_id, user_id, "user", content)


async def append_assistant_message(
    thread_id: str,
    user_id: str,
    content: str,
    citations: Optional[list[dict]] = None,
    status: str = "complete",
) -> Optional[MessageRecord]:
    return await append_message(
        thread_id, user_id, "assistant", content, citations=citations, status=status
    )


async def get_messages(
    thread_id: str,
    max_turns: int = 12,
) -> list[MessageRecord]:
    """Return the most recent messages for a thread, in ascending sequence order."""
    if not is_storage_enabled():
        return []

    container = get_messages_container()
    # Fetch the last max_turns messages ordered by sequence ascending
    query = (
        "SELECT * FROM c WHERE c.thread_id = @thread_id "
        "ORDER BY c.sequence DESC OFFSET 0 LIMIT @limit"
    )
    params = [
        {"name": "@thread_id", "value": thread_id},
        {"name": "@limit", "value": max_turns},
    ]
    try:
        items = []
        async for doc in container.query_items(query=query, parameters=params):
            items.append(_doc_to_message(doc))
        # Reverse to get ascending order (oldest first)
        items.reverse()
        logger.info(
            "chat_store: loaded %d messages | thread=%s", len(items), thread_id
        )
        return items
    except Exception:
        logger.exception(
            "chat_store: failed to load messages | thread=%s", thread_id
        )
        return []
