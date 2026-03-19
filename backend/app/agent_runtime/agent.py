"""AgentRuntime — Microsoft Agent Framework SDK orchestrator.

Pipeline (shared by both streaming and non-streaming paths):

  1. Ensure conversation exists in Cosmos
  2. Persist user message → receive MessageRecord with reserved sequence number
  3. RETRIEVE — hybrid Azure AI Search (keyword + VectorizedQuery)
  4. GATE — abort if evidence count or avg score is below threshold
  5. Get or create AF session (keyed by thread_id)
     └─ Cold-start: load Cosmos history BEFORE the just-saved user message
        (using before_sequence filter) and inject once via CosmosHistoryProvider.
        This prevents the current question from appearing in both the injected
        history block and the active user prompt.
     └─ Warm session: skip history injection; InMemoryHistoryProvider already
        has the in-process turn history.
  6. Inject pre-retrieved results via RagContextProvider
  7. GENERATE via Agent Framework ChatAgent (streaming internally, always)
  8. Emit structured citations from retrieval results (no text-pattern gating)
  9. Persist assistant message + citations

run_stream()  — wraps the pipeline and yields SSE-formatted strings.
run_once()    — wraps the pipeline and returns a plain dict (no SSE).
               Used by POST /chat so the route never parses SSE text.

Cold-start / warm session logging:
  The log lines "cold-start" vs "warm" make it easy to verify in production
  that history injection only fires once per thread per process lifetime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Optional

from agent_framework import AgentSession as AFAgentSession

from app.agent_runtime.citation_provider import build_citations
from app.agent_runtime.history_context_provider import format_history_block
from app.agent_runtime.session import AgentSession
from app.api.schemas import Citation, CitationsPayload
from app.auth.identity import UserIdentity
from app.config.settings import (
    COSMOS_HISTORY_MAX_TURNS,
    MIN_AVG_SCORE,
    MIN_RERANKER_SCORE,
    MIN_RESULTS,
    TOP_K,
    TRACE_MODE,
)
from app.llm.af_agent_factory import af_agent, history_provider, rag_provider
from app.storage import chat_store
from app.storage.cosmos_client import is_storage_enabled
from app.storage.models import MessageRecord
from app.tools.retrieval_tool import retrieve

logger = logging.getLogger(__name__)

# Emit a keepalive ping every N seconds to prevent proxy / browser SSE timeout.
_PING_INTERVAL_SECONDS = 20

# Canned response for the confidence gate failure.
_INSUFFICIENT_EVIDENCE_MSG = (
    "I don't have enough evidence from the technical manuals to answer "
    "your question confidently.\n\n"
    "Could you provide more detail — for example, the equipment name, "
    "model number, or the specific procedure you are looking for?"
)

# Per-process cache of Agent Framework sessions, keyed by thread_id.
# InMemoryHistoryProvider stores turn history in each AFAgentSession.state for
# the lifetime of the process.  On cold start (process restart or new thread),
# Cosmos history is injected once by CosmosHistoryProvider.
_af_sessions: dict[str, AFAgentSession] = {}


# ---------------------------------------------------------------------------
# SSE encoding helpers
# ---------------------------------------------------------------------------

def _sse_data(payload: str) -> str:
    """Encode a string as an SSE data line.

    Newlines in *payload* are escaped to ``\\n`` so SSE's blank-line event
    boundary is never confused with content.  The frontend decodes them back.
    """
    return f"data: {payload.replace(chr(10), chr(92) + 'n')}\n\n"


def _sse_event(event_name: str, payload: str) -> str:
    """Encode a named SSE event."""
    return f"event: {event_name}\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def _compute_gate(results: list[dict]) -> tuple[float, float, bool]:
    """Return (avg_effective_score, gate_threshold, has_reranker).

    When the semantic reranker is active, scores are on the 0–4 scale and we
    gate on MIN_RERANKER_SCORE.  Otherwise we use base RRF/hybrid scores and
    gate on MIN_AVG_SCORE.
    """
    has_reranker = bool(results) and results[0].get("reranker_score") is not None
    if has_reranker:
        avg = sum(r.get("reranker_score") or 0 for r in results) / len(results)
        return avg, MIN_RERANKER_SCORE, True
    avg = sum(r["score"] for r in results) / len(results) if results else 0.0
    return avg, MIN_AVG_SCORE, False


async def _get_or_create_af_session(
    thread_id: str,
    user_id: str,
    current_user_msg: Optional[MessageRecord],
) -> AFAgentSession:
    """Return the AF session for thread_id, hydrating history on cold start.

    Cold-start behavior:
      - A new AFAgentSession is created.
      - Prior Cosmos messages are loaded using before_sequence to exclude the
        just-persisted user message.  This guarantees the current question
        never appears in both the injected history block and the active prompt.
      - The formatted history block is stored in session state via
        CosmosHistoryProvider so it is injected once on the first af_agent.run()
        call.

    Warm session behavior:
      - The existing AFAgentSession is returned as-is.
      - InMemoryHistoryProvider already holds the in-process turn history.
      - No Cosmos history is loaded or injected.
    """
    existing = _af_sessions.get(thread_id)
    if existing is not None:
        logger.info(
            "AgentRuntime: warm session reused | thread=%s", thread_id
        )
        return existing

    # Cold start — create fresh AF session
    af_session = af_agent.create_session()
    _af_sessions[thread_id] = af_session
    logger.info("AgentRuntime: cold-start — new AF session | thread=%s", thread_id)

    if not is_storage_enabled():
        return af_session

    # Determine sequence boundary so the current user question is excluded
    before_seq: Optional[int] = None
    if current_user_msg is not None:
        before_seq = current_user_msg.sequence
        logger.info(
            "AgentRuntime: cold-start history will exclude seq>=%d (current user msg) | thread=%s",
            before_seq, thread_id,
        )

    prior = await chat_store.get_messages(
        thread_id,
        max_turns=COSMOS_HISTORY_MAX_TURNS,
        before_sequence=before_seq,
    )

    if prior:
        block = format_history_block(prior)
        history_provider.store_history_block(af_session, block)
        logger.info(
            "AgentRuntime: injected %d prior message(s) into cold-start session "
            "(before_seq=%s) | thread=%s",
            len(prior), before_seq, thread_id,
        )
    else:
        logger.info(
            "AgentRuntime: no prior history to inject (before_seq=%s) | thread=%s",
            before_seq, thread_id,
        )

    return af_session


async def _buffer_llm_response(
    question: str,
    af_session: AFAgentSession,
) -> tuple[str, bool]:
    """Run the LLM and buffer all streamed tokens.

    Returns (answer_text, had_error).  Uses stream=True internally so the
    code path is identical to run_stream(); only the delivery mechanism
    differs (buffered vs. yielded).
    """
    buf: list[str] = []
    had_error = False
    try:
        async for update in af_agent.run(question, stream=True, session=af_session):
            if update.text:
                buf.append(update.text)
    except Exception:
        logger.exception("LLM generation failed during buffered run")
        had_error = True
    return "".join(buf), had_error


async def _persist_assistant(
    thread_id: str,
    user_id: str,
    answer_text: str,
    citations: list[Citation],
    had_error: bool = False,
) -> None:
    """Persist the assistant message and citations to Cosmos."""
    if not is_storage_enabled() or not answer_text:
        return
    status = "error" if had_error else "complete"
    citations_dicts = [c.model_dump() for c in citations]
    await chat_store.append_assistant_message(
        thread_id, user_id, answer_text, citations=citations_dicts, status=status
    )
    logger.info(
        "AgentRuntime: assistant message persisted | thread=%s citations=%d status=%s",
        thread_id, len(citations_dicts), status,
    )


# ---------------------------------------------------------------------------
# AgentRuntime
# ---------------------------------------------------------------------------

class AgentRuntime:
    """Orchestrates the retrieve → gate → generate → cite → persist pipeline.

    Public API:
      run_stream()  — async generator yielding SSE strings (for /chat/stream).
      run_once()    — coroutine returning a plain dict   (for /chat).

    Both paths share the same business logic; only the delivery layer differs.
    """

    async def run_once(
        self,
        question: str,
        session: AgentSession,
        identity: UserIdentity,
        top_k: int = TOP_K,
    ) -> dict:
        """Run the full pipeline and return the result as a plain dict.

        Returns
        -------
        dict with keys:
          answer       — generated answer text
          citations    — list of citation dicts
          thread_id    — the thread/conversation ID
          session_id   — alias for thread_id (backward compat)
        """
        user_id = identity.user_id
        user_name = identity.user_name
        thread_id = session.session_id

        logger.info(
            "AgentRuntime.run_once | thread=%s user=%s auth=%s | question=%s",
            thread_id, user_id, identity.auth_source, question,
        )

        # ── 1. Ensure conversation exists ─────────────────────────────────
        if is_storage_enabled():
            await chat_store.get_or_create_conversation(thread_id, user_id, user_name)

        # ── 2. Persist user message BEFORE generation ──────────────────────
        user_msg: Optional[MessageRecord] = None
        if is_storage_enabled():
            user_msg = await chat_store.append_user_message(thread_id, user_id, question)

        # ── 3. RETRIEVE ────────────────────────────────────────────────────
        try:
            results: list[dict] = await asyncio.to_thread(retrieve, question, top_k=top_k)
        except Exception:
            logger.exception("Retrieval failed | thread=%s", thread_id)
            err_msg = (
                "I'm sorry — an error occurred while searching the knowledge base. "
                "Please try again."
            )
            await _persist_assistant(thread_id, user_id, err_msg, [], had_error=True)
            return {"answer": err_msg, "citations": [], "thread_id": thread_id, "session_id": thread_id}

        # ── 4. GATE ────────────────────────────────────────────────────────
        avg_effective, gate_threshold, has_reranker = _compute_gate(results)

        if TRACE_MODE:
            logger.info(
                "TRACE | thread=%s n_results=%d avg_effective=%.4f "
                "gate=(>=%d results, >=%.3f) semantic_reranker=%s",
                thread_id, len(results), avg_effective,
                MIN_RESULTS, gate_threshold, has_reranker,
            )

        if len(results) < MIN_RESULTS or avg_effective < gate_threshold:
            logger.info(
                "Gate: insufficient evidence | thread=%s n=%d avg=%.4f "
                "threshold_n=%d threshold=%.3f",
                thread_id, len(results), avg_effective, MIN_RESULTS, gate_threshold,
            )
            await _persist_assistant(thread_id, user_id, _INSUFFICIENT_EVIDENCE_MSG, [])
            return {
                "answer": _INSUFFICIENT_EVIDENCE_MSG,
                "citations": [],
                "thread_id": thread_id,
                "session_id": thread_id,
            }

        # ── 5. AF session (cold-start hydration with before_sequence) ──────
        af_session = await _get_or_create_af_session(thread_id, user_id, user_msg)

        # ── 6. Inject RAG results ──────────────────────────────────────────
        rag_provider.store_results(af_session, results)

        # ── 7. GENERATE (buffered) ─────────────────────────────────────────
        answer_text, had_error = await _buffer_llm_response(question, af_session)

        if had_error:
            err_append = "\n\nI'm sorry — an error occurred while generating the answer. Please try again."
            answer_text = (answer_text + err_append) if answer_text else err_append.strip()

        # ── 8. CITE — always emit from retrieval results when gate passed ──
        citations = build_citations(results)

        # ── 9. Persist assistant message ───────────────────────────────────
        await _persist_assistant(thread_id, user_id, answer_text, citations, had_error=had_error)

        return {
            "answer": answer_text,
            "citations": [c.model_dump() for c in citations],
            "thread_id": thread_id,
            "session_id": thread_id,
        }

    async def run_stream(
        self,
        question: str,
        session: AgentSession,
        identity: UserIdentity,
        top_k: int = TOP_K,
    ) -> AsyncGenerator[str, None]:
        """Execute the pipeline and yield SSE-formatted strings.

        Yields
        ------
        str
            SSE strings: token data lines, named events (citations, ping),
            and the final ``[DONE]`` sentinel.
        """
        user_id = identity.user_id
        user_name = identity.user_name
        thread_id = session.session_id

        logger.info(
            "AgentRuntime.run_stream | thread=%s user=%s auth=%s | question=%s",
            thread_id, user_id, identity.auth_source, question,
        )

        # ── 1. Ensure conversation exists ─────────────────────────────────
        if is_storage_enabled():
            await chat_store.get_or_create_conversation(thread_id, user_id, user_name)

        # ── 2. Persist user message BEFORE generation ──────────────────────
        user_msg: Optional[MessageRecord] = None
        if is_storage_enabled():
            user_msg = await chat_store.append_user_message(thread_id, user_id, question)

        # ── 3. RETRIEVE ────────────────────────────────────────────────────
        try:
            results: list[dict] = await asyncio.to_thread(retrieve, question, top_k=top_k)
        except Exception:
            logger.exception("Retrieval failed | thread=%s", thread_id)
            err_msg = (
                "I'm sorry — an error occurred while searching the knowledge base. "
                "Please try again."
            )
            yield _sse_data(err_msg)
            yield _sse_event("citations", json.dumps({"citations": []}))
            yield _sse_data("[DONE]")
            await _persist_assistant(thread_id, user_id, err_msg, [], had_error=True)
            return

        # ── 4. GATE ────────────────────────────────────────────────────────
        avg_effective, gate_threshold, has_reranker = _compute_gate(results)

        if TRACE_MODE:
            logger.info(
                "TRACE | thread=%s n_results=%d avg_effective=%.4f "
                "gate=(>=%d results, >=%.3f) semantic_reranker=%s",
                thread_id, len(results), avg_effective,
                MIN_RESULTS, gate_threshold, has_reranker,
            )

        if len(results) < MIN_RESULTS or avg_effective < gate_threshold:
            logger.info(
                "Gate: insufficient evidence | thread=%s n=%d avg=%.4f "
                "threshold_n=%d threshold=%.3f",
                thread_id, len(results), avg_effective, MIN_RESULTS, gate_threshold,
            )
            yield _sse_data(_INSUFFICIENT_EVIDENCE_MSG)
            yield _sse_event("citations", CitationsPayload(citations=[]).model_dump_json())
            yield _sse_data("[DONE]")
            await _persist_assistant(thread_id, user_id, _INSUFFICIENT_EVIDENCE_MSG, [])
            return

        # ── 5. AF session (cold-start hydration with before_sequence) ──────
        af_session = await _get_or_create_af_session(thread_id, user_id, user_msg)

        # ── 6. Inject RAG results ──────────────────────────────────────────
        rag_provider.store_results(af_session, results)

        # ── 7. GENERATE — stream tokens via Agent Framework ChatAgent ───────
        last_ping_at = time.monotonic()
        answer_buf: list[str] = []
        stream_error = False
        try:
            async for update in af_agent.run(question, stream=True, session=af_session):
                now = time.monotonic()
                if now - last_ping_at >= _PING_INTERVAL_SECONDS:
                    yield _sse_event("ping", "keepalive")
                    last_ping_at = now

                if update.text:
                    answer_buf.append(update.text)
                    yield _sse_data(update.text)

        except Exception:
            logger.exception("LLM streaming failed | thread=%s", thread_id)
            stream_error = True
            yield _sse_data(
                "\n\nI'm sorry — an error occurred while generating the answer. "
                "Please try again."
            )

        # ── 8. CITE — always emit from retrieval results when gate passed ──
        answer_text = "".join(answer_buf)
        citations = build_citations(results)
        yield _sse_event("citations", CitationsPayload(citations=citations).model_dump_json())
        yield _sse_data("[DONE]")

        # ── 9. Persist assistant message ───────────────────────────────────
        await _persist_assistant(
            thread_id, user_id, answer_text, citations, had_error=stream_error
        )
