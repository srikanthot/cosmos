"""Pydantic request / response models for the chat and conversation APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Chat request / response
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """Incoming chat request body.

    session_id is treated as thread_id for backward compatibility with
    the existing Streamlit frontend.  If omitted, a new thread is created.
    """

    question: str
    session_id: Optional[str] = None


class Citation(BaseModel):
    """A single citation reference with structured metadata."""

    source: str
    title: str = ""
    section: str = ""
    page: str = ""
    url: str = ""
    chunk_id: str = ""


class CitationsPayload(BaseModel):
    """Wrapper for the SSE ``citations`` named event."""

    citations: list[Citation]


# ---------------------------------------------------------------------------
# Conversation management
# ---------------------------------------------------------------------------

class ConversationResponse(BaseModel):
    """Public representation of a conversation thread."""

    thread_id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    last_message_at: Optional[datetime]
    last_user_message_preview: str
    last_assistant_message_preview: str
    message_count: int
    is_deleted: bool


class CreateConversationRequest(BaseModel):
    """Body for POST /conversations."""

    title: Optional[str] = None      # If omitted, defaults to "New Chat"


class UpdateConversationRequest(BaseModel):
    """Body for PATCH /conversations/{thread_id}."""

    title: str


class MessageResponse(BaseModel):
    """Public representation of a single message."""

    id: str
    thread_id: str
    role: str
    content: str
    citations: list[dict[str, Any]]
    created_at: datetime
    sequence: int
    status: str
