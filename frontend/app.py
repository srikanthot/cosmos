"""PSEG Tech Manual Agent — Streamlit frontend.

Layout:
  Sidebar  — New Chat, conversation history, status badge, feedback link
  Main     — Chat area with user/assistant bubbles, citations, input bar

State keys (all initialized in _init_state()):
  current_thread_id       Active thread UUID (str | None)
  conversations           List of conversation dicts from backend
  messages                List of {role, content, citations} for active thread
  _loaded_thread_id       Which thread's messages are currently in `messages`
  _need_conv_refresh      Flag: reload conversation list on next render
  _status_cache           Last health-check result dict
  _status_checked_at      Epoch time of last health check (float)
"""

import time
import uuid
from datetime import datetime, timezone

import streamlit as st
from dotenv import load_dotenv

import api_client as api

load_dotenv()

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
APP_TITLE = "PSEG Tech Manual Agent"
APP_VERSION = "v2.0"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Global CSS
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
/* ── Palette ──────────────────────────────────────────────── */
:root {
    --navy:        #1e3a5f;
    --navy-light:  #2d5a87;
    --orange:      #f26522;
    --bg:          #f7f9fc;
    --card:        #ffffff;
    --border:      #e2e8f0;
    --muted:       #718096;
    --text:        #2d3748;
}

/* ── Page background ──────────────────────────────────────── */
.stApp { background: var(--bg) !important; }

.main .block-container {
    padding: 1.25rem 2rem 2rem !important;
    max-width: 900px;
}

/* ── Chat bubbles ─────────────────────────────────────────── */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatar-user"]) {
    background: linear-gradient(135deg, #eef4fb, #e3ecf7);
    border: 1px solid #d0dff0;
    border-left: 4px solid #4a90d9;
    border-radius: 12px;
    padding: 0.85rem 1.25rem;
    margin: 0.4rem 0;
}

[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatar-assistant"]) {
    background: var(--card);
    border: 1px solid var(--border);
    border-left: 4px solid var(--navy);
    border-radius: 12px;
    padding: 0.85rem 1.25rem;
    margin: 0.4rem 0;
}

/* ── Chat input ───────────────────────────────────────────── */
[data-testid="stChatInput"] {
    border: 2px solid var(--border) !important;
    border-radius: 12px !important;
}

[data-testid="stChatInput"]:focus-within {
    border-color: var(--navy) !important;
    box-shadow: 0 0 0 3px rgba(30,58,95,0.08) !important;
}

/* ── Sidebar ──────────────────────────────────────────────── */
[data-testid="stSidebar"] {
    background: var(--card) !important;
    border-right: 1px solid var(--border);
}

/* ── Sidebar conversation item buttons ────────────────────── */
[data-testid="stSidebar"] .stButton > button {
    border-radius: 8px;
    font-size: 0.83rem;
    text-align: left;
    justify-content: flex-start;
    padding: 6px 10px;
    line-height: 1.4;
    height: auto;
    white-space: pre-wrap;
}

/* ── Spinner ──────────────────────────────────────────────── */
.stSpinner > div { border-top-color: var(--orange) !important; }

/* ── Divider ──────────────────────────────────────────────── */
hr { border: none; height: 1px; background: var(--border); margin: 0.75rem 0; }

/* ── Citation expander ────────────────────────────────────── */
[data-testid="stExpander"] {
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
    background: #fafbfd !important;
}
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _init_state() -> None:
    """Initialize all session state keys once per Streamlit session."""
    defaults: dict = {
        "current_thread_id": None,
        "conversations": [],
        "messages": [],
        "_loaded_thread_id": None,
        "_need_conv_refresh": True,   # load on first render
        "_status_cache": {"ok": None, "storage": "unknown", "error": "checking"},
        "_status_checked_at": 0.0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


def _relative_time(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        diff = datetime.now(timezone.utc) - dt
        secs = diff.total_seconds()
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        if diff.days < 7:
            return f"{diff.days}d ago"
        return dt.strftime("%b %d")
    except Exception:
        return ""


def _backend_msgs_to_state(backend_msgs: list[dict]) -> list[dict]:
    return [
        {
            "role": m.get("role", "user"),
            "content": m.get("content", ""),
            "citations": m.get("citations") or [],
        }
        for m in backend_msgs
    ]


# ---------------------------------------------------------------------------
# State actions
# ---------------------------------------------------------------------------

def _refresh_conversations() -> None:
    st.session_state.conversations = api.list_conversations()
    st.session_state._need_conv_refresh = False


def _load_thread_messages(thread_id: str) -> None:
    backend_msgs = api.get_messages(thread_id)
    st.session_state.messages = _backend_msgs_to_state(backend_msgs)
    st.session_state._loaded_thread_id = thread_id


def _select_thread(thread_id: str) -> None:
    st.session_state.current_thread_id = thread_id
    st.session_state._loaded_thread_id = None  # force reload on next render
    st.session_state.messages = []


def _new_chat() -> None:
    """Create a new thread UUID locally; backend creates the record on first message."""
    new_id = str(uuid.uuid4())
    st.session_state.current_thread_id = new_id
    st.session_state._loaded_thread_id = new_id  # nothing to load from backend yet
    st.session_state.messages = []


def _delete_thread(thread_id: str) -> None:
    api.delete_conversation(thread_id)
    if st.session_state.current_thread_id == thread_id:
        st.session_state.current_thread_id = None
        st.session_state._loaded_thread_id = None
        st.session_state.messages = []
    st.session_state._need_conv_refresh = True


def _get_backend_status() -> dict:
    """Cached backend health status (refreshes every 30 s)."""
    now = time.time()
    if now - st.session_state._status_checked_at > 30:
        st.session_state._status_cache = api.check_health()
        st.session_state._status_checked_at = now
    return st.session_state._status_cache


# ---------------------------------------------------------------------------
# PSEG logo SVG
# ---------------------------------------------------------------------------
_LOGO_HTML = f"""
<div style="text-align:center; padding:0.5rem 0 0.25rem;">
  <svg viewBox="0 0 160 50" xmlns="http://www.w3.org/2000/svg"
       style="height:42px; width:auto;">
    <circle cx="25" cy="25" r="22" fill="#f26522"/>
    <g fill="white">
      <polygon points="25,7 26.4,15 23.6,15"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(30,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(60,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(90,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(120,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(150,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(180,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(210,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(240,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(270,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(300,25,25)"/>
      <polygon points="25,7 26.4,15 23.6,15" transform="rotate(330,25,25)"/>
    </g>
    <circle cx="25" cy="25" r="5.5" fill="white"/>
    <text x="54" y="33" font-family="Arial,sans-serif" font-size="24"
          font-weight="bold" fill="#1e3a5f">PSEG</text>
  </svg>
  <div style="font-size:0.72rem; color:#718096; margin-top:2px;">
    Tech Manual Agent &nbsp;·&nbsp; {APP_VERSION}
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# Rendering — sidebar
# ---------------------------------------------------------------------------

def _render_status_badge(status: dict) -> None:
    if status.get("ok") is None:
        st.caption("⏳ Checking backend…")
    elif status["ok"]:
        storage = status.get("storage", "unknown")
        st.success(f"● Connected  ({storage})")
    else:
        err = status.get("error", "unknown error")
        st.error(f"● Disconnected — {err}")


def _render_conversation_list() -> None:
    conversations = st.session_state.conversations
    current = st.session_state.current_thread_id

    if not conversations:
        st.markdown(
            "<div style='font-size:0.80rem; color:#a0aec0; padding:6px 4px;'>"
            "No conversations yet.<br>Click <b>＋ New Chat</b> to begin."
            "</div>",
            unsafe_allow_html=True,
        )
        return

    for conv in conversations:
        thread_id = conv.get("thread_id", "")
        title = conv.get("title") or "Untitled"
        last_at = conv.get("last_message_at") or conv.get("updated_at") or ""
        rel_time = _relative_time(last_at)
        is_selected = thread_id == current

        prefix = "▶ " if is_selected else "   "
        short_title = _truncate(title, 30)
        label = f"{prefix}{short_title}"
        if rel_time:
            label += f"\n     {rel_time}"

        col_btn, col_del = st.columns([7, 1])
        with col_btn:
            btn_type = "primary" if is_selected else "secondary"
            if st.button(label, key=f"thread_{thread_id}", use_container_width=True, type=btn_type):
                if not is_selected:
                    _select_thread(thread_id)
                    st.rerun()
        with col_del:
            if st.button("✕", key=f"del_{thread_id}", help="Delete conversation"):
                _delete_thread(thread_id)
                st.rerun()


def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown(_LOGO_HTML, unsafe_allow_html=True)
        st.markdown("---")

        # Backend status
        _render_status_badge(_get_backend_status())
        st.markdown("---")

        # New Chat
        if st.button("＋  New Chat", use_container_width=True, type="primary"):
            _new_chat()
            st.rerun()

        # Recent chats header + refresh
        conv_count = len(st.session_state.conversations)
        col_h, col_r = st.columns([5, 1])
        with col_h:
            st.markdown(
                f"<div style='font-size:0.78rem; font-weight:600; color:#4a5568; "
                f"padding:8px 2px 4px;'>Recent Chats ({conv_count})</div>",
                unsafe_allow_html=True,
            )
        with col_r:
            if st.button("↻", key="refresh_convs", help="Refresh list"):
                st.session_state._need_conv_refresh = True
                st.rerun()

        _render_conversation_list()

        st.markdown("---")

        # Feedback link
        if api.FEEDBACK_URL:
            st.markdown(
                f'<a href="{api.FEEDBACK_URL}" target="_blank">'
                f'<button style="width:100%;padding:8px;border:none;border-radius:8px;'
                f'background:#f26522;color:white;font-weight:600;cursor:pointer;'
                f'font-size:0.83rem;">📝 Share Feedback</button></a>',
                unsafe_allow_html=True,
            )
            st.markdown("")

        # Dev mode indicator
        if api.DEBUG_USER_ID:
            st.markdown(
                f"<div style='font-size:0.72rem; color:#a0aec0; border:1px solid #e2e8f0;"
                f"border-radius:6px; padding:5px 8px; margin-top:4px;'>"
                f"🔧 Testing as <code>{api.DEBUG_USER_ID}</code>"
                f"</div>",
                unsafe_allow_html=True,
            )


# ---------------------------------------------------------------------------
# Rendering — main chat area
# ---------------------------------------------------------------------------

def _active_thread_title() -> str:
    cid = st.session_state.current_thread_id
    if not cid:
        return "Tech Manual Agent"
    for conv in st.session_state.conversations:
        if conv.get("thread_id") == cid:
            return conv.get("title") or "New Chat"
    return "New Chat"


def _render_header() -> None:
    title = _active_thread_title()
    st.markdown(
        f"""
<div style="background:linear-gradient(135deg,#1e3a5f 0%,#2d5a87 100%);
            border-radius:14px; padding:1rem 1.75rem; margin-bottom:1rem;
            border-bottom:3px solid #f26522;
            box-shadow:0 4px 16px rgba(30,58,95,0.10);">
  <div style="font-size:1.1rem; font-weight:700; color:white; margin-bottom:3px;">
    ⚡ {title}
  </div>
  <div style="font-size:0.80rem; color:rgba(255,255,255,0.80);">
    Ask questions against PSEG technical documentation — answers are grounded in
    retrieved manual content with source citations.
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_empty_state() -> None:
    st.markdown(
        """
<div style="text-align:center; padding:5rem 2rem 3rem; color:#718096;">
  <div style="font-size:3.5rem; margin-bottom:1rem;">💬</div>
  <div style="font-size:1.25rem; font-weight:600; color:#2d3748; margin-bottom:0.5rem;">
    Start a conversation
  </div>
  <div style="font-size:0.90rem; max-width:460px; margin:0 auto; line-height:1.7;">
    Click <strong>＋ New Chat</strong> in the sidebar, then ask a question
    about PSEG technical manuals.<br><br>
    Old conversations are saved automatically and available in the sidebar.
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_citations(citations: list) -> None:
    if not citations:
        return
    with st.expander(f"📚 Sources ({len(citations)})", expanded=False):
        for i, c in enumerate(citations, 1):
            source = c.get("source", "Unknown")
            title = c.get("title") or source
            section = c.get("section", "")
            page = c.get("page", "")
            url = c.get("url", "")
            chunk_id = c.get("chunk_id", "")

            lines = [f"**{i}. {title}**"]
            if title != source:
                lines.append(f"*File: {source}*")
            if section:
                lines.append(f"> {section}")
            if page not in ("", None):
                lines.append(f"Page {page}")
            if chunk_id:
                lines.append(f"Chunk `{chunk_id}`")

            md = "  \n".join(lines)
            if url:
                md += f"  \n[View source]({url})"
            st.markdown(md)

            if i < len(citations):
                st.markdown(
                    "<hr style='margin:6px 0; opacity:0.3;'>", unsafe_allow_html=True
                )


def _render_messages() -> None:
    for msg in st.session_state.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        citations = msg.get("citations") or []
        with st.chat_message(role):
            st.markdown(content)
            if role == "assistant" and citations:
                _render_citations(citations)


# ---------------------------------------------------------------------------
# Send handler
# ---------------------------------------------------------------------------

def _format_send_error(exc: Exception) -> str:
    import requests as _req

    if isinstance(exc, _req.exceptions.ConnectionError):
        return f"Cannot reach backend at `{api.BACKEND_URL}`. Is it running?"
    if isinstance(exc, _req.exceptions.Timeout):
        return "Request timed out — the backend may still be processing. Try again."
    return f"Error: {exc}"


def _handle_send(question: str) -> None:
    thread_id = st.session_state.current_thread_id

    # Add user message to state immediately (already rendered above input)
    st.session_state.messages.append({"role": "user", "content": question, "citations": []})

    with st.chat_message("assistant"):
        with st.spinner("Getting answer…"):
            try:
                answer, citations, returned_thread_id = api.send_message(question, thread_id)

                # Keep thread_id in sync if backend assigned one
                if returned_thread_id and returned_thread_id != thread_id:
                    st.session_state.current_thread_id = returned_thread_id
                    st.session_state._loaded_thread_id = returned_thread_id

                st.markdown(answer)
                if citations:
                    _render_citations(citations)

                st.session_state.messages.append(
                    {"role": "assistant", "content": answer, "citations": citations}
                )

            except Exception as exc:
                err_msg = _format_send_error(exc)
                st.error(err_msg)
                st.session_state.messages.append(
                    {"role": "assistant", "content": err_msg, "citations": []}
                )

    # Refresh conversation list so updated title/preview appears in sidebar
    st.session_state._need_conv_refresh = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _init_state()

    # Refresh conversation list if flagged (first load + after each message)
    if st.session_state._need_conv_refresh:
        _refresh_conversations()

    # Load messages when the active thread changes
    cid = st.session_state.current_thread_id
    lid = st.session_state._loaded_thread_id
    if cid and cid != lid:
        with st.spinner("Loading conversation…"):
            _load_thread_messages(cid)

    # Sidebar (rendered before main content)
    _render_sidebar()

    # Main area
    _render_header()

    if not st.session_state.current_thread_id:
        _render_empty_state()
    else:
        _render_messages()

    # Chat input — only when a thread is active
    if st.session_state.current_thread_id:
        if prompt := st.chat_input("Ask a question about PSEG technical manuals…"):
            with st.chat_message("user"):
                st.markdown(prompt)
            _handle_send(prompt)
            st.rerun()
    else:
        st.markdown(
            "<div style='text-align:center; padding:0.5rem 0; font-size:0.85rem; "
            "color:#a0aec0;'>← Create or select a chat to start</div>",
            unsafe_allow_html=True,
        )

    # Footer
    st.markdown(
        "<div style='text-align:center; padding:1.5rem 0 0.5rem; color:#a0aec0; "
        "font-size:0.75rem;'>"
        "PSEG Tech Manual Agent &nbsp;·&nbsp; Powered by Azure AI &nbsp;·&nbsp; GCC High"
        "</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
