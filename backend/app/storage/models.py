"""Cosmos DB document models for conversations and messages.

ConversationRecord — one document per chat thread, partitioned by user_id.
MessageRecord      — one document per chat turn, partitioned by thread_id.

Both models serialize cleanly to/from Cosmos JSON via Pydantic v2.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class ConversationRecord(BaseModel):
    """Represents a single chat thread owned by a user."""

    # Cosmos DB requires 'id' as the document key.
    # We use thread_id as both id and the logical key so lookups are direct reads.
    id: str = Field(default_factory=_new_id)
    thread_id: str = Field(default="")           # set equal to id after creation
    user_id: str
    user_name: str = ""
    title: str = "New Chat"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_message_at: Optional[datetime] = None
    last_user_message_preview: str = ""
    last_assistant_message_preview: str = ""
    message_count: int = 0
    is_deleted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    def model_post_init(self, __context: Any) -> None:
        # Ensure thread_id always mirrors id
        if not self.thread_id:
            self.thread_id = self.id


class MessageRecord(BaseModel):
    """Represents a single user or assistant message within a thread."""

    id: str = Field(default_factory=_new_id)
    thread_id: str
    user_id: str
    role: str                    # "user" | "assistant"
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    sequence: int = 0            # ascending within thread; used for ordering
    status: str = "complete"     # "complete" | "partial" | "error"
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}
