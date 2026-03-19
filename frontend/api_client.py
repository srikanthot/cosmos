"""Backend API service layer.

All calls to the backend are centralized here.  The Streamlit UI imports from
this module only — no requests.get/post calls live in app.py.

Configuration (read from environment / .env):
  BACKEND_URL   — base URL of the FastAPI backend (default: http://localhost:8000)
  DEBUG_USER_ID — if set, sent as X-Debug-User-Id header (simulates a named user)
  FEEDBACK_URL  — optional link shown in sidebar

Error handling philosophy:
  - Health check: returns structured dict, never raises
  - List/read endpoints: return [] or None on failure, never raise
  - send_message: raises RuntimeError so the caller can surface it to the user
  - delete/rename: return bool success flag
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_raw = os.getenv("BACKEND_URL", "").strip().rstrip("/")
if not _raw:
    _port = os.getenv("BACKEND_PORT", "8000")
    _raw = f"http://localhost:{_port}"

BACKEND_URL: str = _raw
FEEDBACK_URL: str = os.getenv("FEEDBACK_URL", "").strip()
DEBUG_USER_ID: str = os.getenv("DEBUG_USER_ID", "").strip()

# Timeouts (seconds)
_T_HEALTH = 4
_T_LIST = 10
_T_CHAT = 120


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    h: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if DEBUG_USER_ID:
        h["X-Debug-User-Id"] = DEBUG_USER_ID
    return h


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def check_health() -> dict:
    """Check backend /health.

    Returns:
        {"ok": bool, "storage": str, "error": str|None}
    """
    try:
        r = requests.get(f"{BACKEND_URL}/health", headers=_headers(), timeout=_T_HEALTH)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "storage": data.get("storage", "unknown"), "error": None}
        return {"ok": False, "storage": "unknown", "error": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "storage": "unknown", "error": "unreachable"}
    except Exception as exc:
        return {"ok": False, "storage": "unknown", "error": str(exc)[:100]}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def list_conversations(limit: int = 40) -> list[dict]:
    """Return recent conversations for the current user.

    Fields per item: thread_id, title, last_message_at, message_count,
    last_user_message_preview, last_assistant_message_preview, is_deleted.

    Returns [] on any failure.
    """
    try:
        r = requests.get(
            f"{BACKEND_URL}/conversations",
            headers=_headers(),
            params={"limit": limit},
            timeout=_T_LIST,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


def create_conversation(title: str | None = None) -> dict | None:
    """Create a new conversation thread on the backend.

    Returns conversation dict (including thread_id) or None on failure.
    """
    try:
        body: dict = {}
        if title:
            body["title"] = title
        r = requests.post(
            f"{BACKEND_URL}/conversations",
            json=body,
            headers=_headers(),
            timeout=_T_LIST,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_messages(thread_id: str, limit: int = 100) -> list[dict]:
    """Return ordered messages for a thread.

    Fields per item: id, role, content, citations, created_at, sequence, status.

    Returns [] on any failure.
    """
    try:
        r = requests.get(
            f"{BACKEND_URL}/conversations/{thread_id}/messages",
            headers=_headers(),
            params={"limit": limit},
            timeout=_T_LIST,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


def delete_conversation(thread_id: str) -> bool:
    """Soft-delete a conversation.  Returns True on success."""
    try:
        r = requests.delete(
            f"{BACKEND_URL}/conversations/{thread_id}",
            headers=_headers(),
            timeout=_T_LIST,
        )
        return r.status_code == 200
    except Exception:
        return False


def rename_conversation(thread_id: str, title: str) -> bool:
    """Rename a conversation thread.  Returns True on success."""
    try:
        r = requests.patch(
            f"{BACKEND_URL}/conversations/{thread_id}",
            json={"title": title},
            headers=_headers(),
            timeout=_T_LIST,
        )
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def send_message(question: str, thread_id: str) -> tuple[str, list, str]:
    """Send a question to the backend and return (answer, citations, thread_id).

    Raises:
        RuntimeError:   on HTTP 4xx/5xx from backend
        requests.*:     on network failures (ConnectionError, Timeout, etc.)
    """
    r = requests.post(
        f"{BACKEND_URL}/chat",
        json={"question": question, "session_id": thread_id},
        headers=_headers(),
        timeout=_T_CHAT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Backend returned HTTP {r.status_code}:\n{r.text[:600]}")

    data = r.json()
    answer = (data.get("answer") or "").strip() or "No answer returned from backend."
    citations = data.get("citations") or []
    returned_thread_id = (
        data.get("thread_id") or data.get("session_id") or thread_id
    )
    return answer, citations, returned_thread_id
