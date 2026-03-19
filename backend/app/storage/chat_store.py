"""Chat storage layer — all Cosmos DB operations for conversations and messages.

All public functions are async and return None / [] gracefully when storage is
disabled (no COSMOS_ENDPOINT configured).  Routes and AgentRuntime call these
functions directly — no Cosmos SDK calls are scattered elsewhere.

Container layout:
  conversations  — partitioned by /user_id
  messages       — partitioned by /thread_id

Multi-user isolation guarantees:
  - get_messages_for_user() validates conversation ownership before querying
    and filters message results by both thread_id AND user_id.  A user can
    never read another user's messages even if they somehow know the thread_id.
  - _append_message() reads the conversation using (thread_id, partition_key=user_id).
    If the conversation does not exist under that user's partition Cosmos returns
    404 and the append is rejected — a user can never write into another user's
    thread even if they present the correct thread_id.
  - create_conversation() uses upsert semantics.  Two users can theoretically
    hold conversation documents with the same thread_id in different partitions
    (user_id A and user_id B).  This is harmless because message reads/writes
    are always scoped to the calling user's user_id.

Sequence safety:
  _append_message() uses optimistic concurrency (CAS) on the conversation
  document's _etag to atomically reserve the next sequence number and update
  metadata.  On a concurrent write conflict (HTTP 412) it retries up to
  _MAX_CAS_RETRIES times before giving up.  This prevents duplicate sequence
  numbers under concurrent requests for the same thread.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from azure.core.conditions import MatchConditions
from azure.cosmos.exceptions import CosmosHttpResponseError

from app.storage.cosmos_client import (
    get_conversations_container,
    get_messages_container,
    is_storage_enabled,
)
from app.storage.models import ConversationRecord, MessageRecord

logger = logging.getLogger(__name__)

_PREVIEW_MAX_CHARS = 120
_MAX_CAS_RETRIES = 5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _preview(text: str) -> str:
    """Truncate to _PREVIEW_MAX_CHARS at a word boundary."""
    text = text.strip()
    if len(text) <= _PREVIEW_MAX_CHARS:
        return text
    return text[:_PREVIEW_MAX_CHARS].rsplit(" ", 1)[0] + "…"


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

    q = q[0].upper() + q[1:]

    if len(q) > 70:
        q = q[:67].rsplit(" ", 1)[0] + "…"

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
        await container.upsert_item(body=conv.model_dump(mode="json"))
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
    """Read a conversation by thread_id scoped to user_id.

    Returns None if not found (including when the thread_id exists but belongs
    to a different user — Cosmos partition isolation prevents cross-user reads).
    """
    if not is_storage_enabled():
        return None

    container = get_conversations_container()
    try:
        doc = await container.read_item(item=thread_id, partition_key=user_id)
        return _doc_to_conversation(doc)
    except CosmosHttpResponseError as exc:
        if exc.status_code != 404:
            logger.warning(
                "chat_store: failed to read conversation | thread=%s user=%s | %s",
                thread_id, user_id, exc,
            )
        return None
    except Exception as exc:
        logger.warning(
            "chat_store: failed to read conversation | thread=%s user=%s | %s",
            thread_id, user_id, exc,
        )
        return None


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
        async for doc in container.query_items(query=query, parameters=params):
            items.append(_doc_to_conversation(doc))
        logger.info("chat_store: listed %d conversations | user=%s", len(items), user_id)
        return items
    except Exception:
        logger.exception("chat_store: failed to list conversations | user=%s", user_id)
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
            "chat_store: conversation soft-deleted | thread=%s user=%s", thread_id, user_id
        )
        return True
    except Exception:
        logger.exception(
            "chat_store: failed to soft-delete conversation | thread=%s", thread_id
        )
        return False


async def update_conversation_title(thread_id: str, user_id: str, title: str) -> bool:
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
        logger.info("chat_store: title updated | thread=%s title=%r", thread_id, title)
        return True
    except Exception:
        logger.exception("chat_store: failed to update title | thread=%s", thread_id)
        return False


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------

async def _append_message(
    thread_id: str,
    user_id: str,
    role: str,
    content: str,
    citations: Optional[list[dict]] = None,
    status: str = "complete",
) -> Optional[MessageRecord]:
    """Atomically reserve the next sequence number and persist a message.

    Ownership enforcement:
      Step 1 reads the conversation document with partition_key=user_id.  If the
      conversation does not exist under this user's partition, Cosmos returns 404
      and we return None.  This prevents a user from writing into a thread they
      do not own even if they present the correct thread_id.

    CAS (compare-and-swap) loop:
      1. Read the conversation document and capture its _etag.
      2. Compute next sequence = message_count + 1 and update all metadata fields.
      3. replace_item() with IfNotModified condition — atomically commits the
         incremented count and updated metadata.
      4. If another writer concurrently modified the document (HTTP 412), retry
         from step 1 up to _MAX_CAS_RETRIES times.
      5. Once the sequence slot is reserved, upsert the message document.
         Message documents use UUID ids so concurrent messages never collide.

    If the message upsert fails after a successful sequence reservation, the
    sequence slot is lost (message_count is ahead by one) — this is logged
    explicitly so it can be detected and the count corrected if needed.
    """
    if not is_storage_enabled():
        return None

    conv_container = get_conversations_container()
    msg_container = get_messages_container()

    for attempt in range(_MAX_CAS_RETRIES):
        # ── Step 1: Read conversation with its current etag ───────────────
        # partition_key=user_id enforces ownership: 404 if thread is not owned
        # by this user.
        try:
            doc = await conv_container.read_item(item=thread_id, partition_key=user_id)
        except CosmosHttpResponseError as exc:
            if exc.status_code == 404:
                logger.error(
                    "chat_store: conversation not found for message append "
                    "(ownership check) | thread=%s user=%s",
                    thread_id, user_id,
                )
                return None
            logger.exception(
                "chat_store: error reading conversation for append | thread=%s user=%s",
                thread_id, user_id,
            )
            return None

        etag: str | None = doc.get("_etag")
        current_count: int = doc.get("message_count", 0)
        sequence: int = current_count + 1
        now_iso = _utcnow_iso()

        # ── Step 2: Apply metadata updates to the in-memory doc ───────────
        is_first_user_msg = (role == "user" and current_count == 0)
        preview = _preview(content)

        doc["message_count"] = sequence
        doc["updated_at"] = now_iso
        doc["last_message_at"] = now_iso

        if role == "user":
            doc["last_user_message_preview"] = preview
            # Auto-generate title on first user message
            if is_first_user_msg and doc.get("title") == "New Chat":
                doc["title"] = generate_title(content)
        else:
            doc["last_assistant_message_preview"] = preview

        # ── Step 3: Atomic CAS update — reserve the sequence slot ─────────
        try:
            replace_kwargs: dict = {}
            if etag:
                replace_kwargs["match_condition"] = MatchConditions.IfNotModified
                replace_kwargs["etag"] = etag

            await conv_container.replace_item(
                item=thread_id,
                body=doc,
                **replace_kwargs,
            )

        except CosmosHttpResponseError as exc:
            if exc.status_code == 412 and attempt < _MAX_CAS_RETRIES - 1:
                # Concurrent modification — retry with fresh read
                logger.debug(
                    "chat_store: CAS conflict on seq reservation, retrying "
                    "(attempt=%d/%d) | thread=%s",
                    attempt + 1, _MAX_CAS_RETRIES, thread_id,
                )
                continue
            logger.exception(
                "chat_store: failed to reserve sequence after %d attempt(s) | thread=%s",
                attempt + 1, thread_id,
            )
            return None

        # ── Step 4: Sequence reserved — persist the message document ──────
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
            await msg_container.upsert_item(body=msg.model_dump(mode="json"))
            logger.info(
                "chat_store: message saved | thread=%s user=%s role=%s seq=%d len=%d",
                thread_id, user_id, role, sequence, len(content),
            )
            return msg
        except Exception:
            logger.exception(
                "chat_store: message save failed after sequence reservation — "
                "sequence slot %d will be unused | thread=%s user=%s",
                sequence, thread_id, user_id,
            )
            return None

    # Exhausted all retries
    logger.error(
        "chat_store: exhausted %d CAS retries for sequence reservation | thread=%s",
        _MAX_CAS_RETRIES, thread_id,
    )
    return None


async def append_user_message(
    thread_id: str,
    user_id: str,
    content: str,
) -> Optional[MessageRecord]:
    """Append a user message and atomically update conversation metadata."""
    return await _append_message(thread_id, user_id, "user", content)


async def append_assistant_message(
    thread_id: str,
    user_id: str,
    content: str,
    citations: Optional[list[dict]] = None,
    status: str = "complete",
) -> Optional[MessageRecord]:
    """Append an assistant message and atomically update conversation metadata."""
    return await _append_message(
        thread_id, user_id, "assistant", content,
        citations=citations, status=status,
    )


async def get_messages_for_user(
    thread_id: str,
    user_id: str,
    max_turns: int = 12,
    before_sequence: Optional[int] = None,
) -> list[MessageRecord]:
    """Return messages for a thread, scoped to the owning user.

    Ownership is validated first: if the conversation does not exist for
    user_id, an empty list is returned and a warning is logged.

    Messages are additionally filtered by user_id in the query itself
    (WHERE thread_id = ? AND user_id = ?) as a second layer of isolation.
    This ensures that even in the theoretical case of two users having
    conversation documents with the same thread_id (in different Cosmos
    partitions), each user only sees their own messages.

    Parameters
    ----------
    thread_id:
        The conversation thread to query.
    user_id:
        The requesting user.  Must match the conversation owner.
    max_turns:
        Maximum number of messages to return (most recent N).
    before_sequence:
        When set, only messages with sequence < before_sequence are returned.
        Used by AgentRuntime cold-start hydration to exclude the current
        user message that was just persisted, preventing it from appearing
        in both the injected history block and the active user prompt.
    """
    if not is_storage_enabled():
        return []

    # ── Ownership check ───────────────────────────────────────────────────
    conv = await get_conversation(thread_id, user_id)
    if conv is None:
        logger.warning(
            "chat_store: get_messages_for_user denied — conversation not found "
            "for this user | thread=%s user=%s",
            thread_id, user_id,
        )
        return []

    container = get_messages_container()

    # Build query with mandatory user_id filter and optional sequence filter
    seq_clause = ""
    params: list[dict] = [
        {"name": "@thread_id", "value": thread_id},
        {"name": "@user_id",   "value": user_id},
    ]

    if before_sequence is not None:
        seq_clause = "AND c.sequence < @before_sequence"
        params.append({"name": "@before_sequence", "value": before_sequence})

    params.append({"name": "@limit", "value": max_turns})

    query = (
        f"SELECT * FROM c "
        f"WHERE c.thread_id = @thread_id AND c.user_id = @user_id {seq_clause} "
        f"ORDER BY c.sequence DESC OFFSET 0 LIMIT @limit"
    )

    try:
        items = []
        async for doc in container.query_items(query=query, parameters=params):
            items.append(_doc_to_message(doc))
        # Reverse to restore ascending (chronological) order
        items.reverse()
        logger.info(
            "chat_store: loaded %d messages | thread=%s user=%s before_seq=%s",
            len(items), thread_id, user_id, before_sequence,
        )
        return items
    except Exception:
        logger.exception(
            "chat_store: failed to load messages | thread=%s user=%s",
            thread_id, user_id,
        )
        return []
