"""AgentRuntime — Microsoft Agent Framework SDK orchestrator.

Architecture (replaces the hand-rolled LLM loop with official SDK primitives):

  POST /chat/stream
       ↓
  routes.py              thin: validate → create session → call runtime
       ↓
  AgentRuntime.run_stream()
    1. resolve_identity()  derive user_id from request headers
    2. resolve thread      use session_id as thread_id; create if new
    3. persist user msg    save to Cosmos before generation
    4. retrieve()          embed query → hybrid Azure AI Search (VectorizedQuery)
    5. GATE                abort early if evidence count or avg score too low
    6. hydrate session     load Cosmos history into AF session if cold-start
    7. rag_provider        store pre-retrieved results in session.state
    8. af_agent.run()      Agent Framework ChatAgent → AzureOpenAIChatClient
                             • InMemoryHistoryProvider  multi-turn memory
                             • CosmosHistoryProvider    cold-start history injection
                             • RagContextProvider       injects chunks
                             • LLM streams tokens via ResponseStream
    9. SSE stream          yield tokens + keepalive pings
   10. CitationProvider    dedup + emit structured citations event
   11. persist assistant   save answer + citations to Cosmos after generation
       ↓
  SSE stream → Streamlit UI

Why Agent Framework?
  - AzureOpenAIChatClient owns the Azure OpenAI connection (API-key auth).
  - ChatAgent (via as_agent()) handles prompt assembly, history, and streaming.
  - RagContextProvider.before_run() is the official SDK hook for RAG injection.
  - CosmosHistoryProvider.before_run() injects cold-start Cosmos history once.
  - InMemoryHistoryProvider maintains multi-turn memory for the process lifetime.
  - Azure AI Foundry Managed Agents are unavailable in GCC High — this pattern
    gives the same architecture without the managed service.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

from agent_framework import AgentSession as AFAgentSession

from app.agent_runtime.citation_provider import build_citations
from app.agent_runtime.history_context_provider import format_history_block
from app.agent_runtime.session import AgentSession
from app.api.schemas import CitationsPayload
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
from app.tools.retrieval_tool import retrieve

logger = logging.getLogger(__name__)

# Emit a keepalive ping every N seconds to prevent proxy / browser SSE timeout.
_PING_INTERVAL_SECONDS = 20

# Per conversation-session cache of Agent Framework sessions.
# Keyed by thread_id (our AgentSession.session_id).
# InMemoryHistoryProvider stores message history inside each AFAgentSession.state,
# giving multi-turn memory for the lifetime of the process.
# When the process restarts this dict is empty; Cosmos history is re-injected
# by CosmosHistoryProvider on the first call for each cold-started thread.
_af_sessions: dict[str, AFAgentSession] = {}


def _sse_data(payload: str) -> str:
    """Encode a string as an SSE data line.

    Newlines inside *payload* are replaced by the literal ``\\n`` so SSE's
    blank-line event boundary is never confused with content newlines.
    The Streamlit frontend decodes them back before rendering.
    """
    return f"data: {payload.replace(chr(10), chr(92) + 'n')}\n\n"


def _sse_event(event_name: str, payload: str) -> str:
    """Encode a named SSE event."""
    return f"event: {event_name}\ndata: {payload}\n\n"


class AgentRuntime:
    """Orchestrates the full retrieve → gate → generate → cite → persist pipeline.

    Uses the Microsoft Agent Framework SDK for LLM invocation, context
    injection (RagContextProvider, CosmosHistoryProvider), and conversation
    memory (InMemoryHistoryProvider).
    """

    async def run_stream(
        self,
        question: str,
        session: AgentSession,
        identity: UserIdentity,
        top_k: int = TOP_K,
    ) -> AsyncGenerator[str, None]:
        """Execute the pipeline and yield SSE-formatted strings.

        This is an async generator — pass it directly to FastAPI's
        StreamingResponse.  Each yielded string is a complete SSE line.

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

        # ── 1. Ensure conversation exists in Cosmos ───────────────────────────
        if is_storage_enabled():
            await chat_store.get_or_create_conversation(thread_id, user_id, user_name)

        # ── 2. Persist user message BEFORE generation ─────────────────────────
        if is_storage_enabled():
            await chat_store.append_user_message(thread_id, user_id, question)

        # ── 3. RETRIEVE — hybrid Azure AI Search (keyword + VectorizedQuery) ──
        # Runs in a thread to avoid blocking the async event loop.
        try:
            results: list[dict] = await asyncio.to_thread(
                retrieve, question, top_k=top_k
            )
        except Exception:
            logger.exception("Retrieval failed | thread=%s", thread_id)
            yield _sse_data(
                "I'm sorry — an error occurred while searching the knowledge base. "
                "Please try again."
            )
            yield _sse_event("citations", json.dumps({"citations": []}))
            yield _sse_data("[DONE]")
            return

        # ── 4. GATE — confidence check ────────────────────────────────────────
        has_reranker = bool(results) and results[0].get("reranker_score") is not None
        if has_reranker:
            avg_effective = (
                sum(r.get("reranker_score") or 0 for r in results) / len(results)
            )
            gate_threshold = MIN_RERANKER_SCORE
        else:
            avg_effective = (
                sum(r["score"] for r in results) / len(results) if results else 0.0
            )
            gate_threshold = MIN_AVG_SCORE

        if TRACE_MODE:
            logger.info(
                "TRACE | thread=%s n_results=%d  avg_effective=%.4f  "
                "gate=(>=%d results, >=%.3f)  semantic_reranker=%s",
                thread_id, len(results), avg_effective,
                MIN_RESULTS, gate_threshold, has_reranker,
            )

        if len(results) < MIN_RESULTS or avg_effective < gate_threshold:
            logger.info(
                "Gate: insufficient evidence | thread=%s n=%d avg=%.4f "
                "threshold_n=%d threshold=%.3f",
                thread_id, len(results), avg_effective, MIN_RESULTS, gate_threshold,
            )
            insufficient_msg = (
                "I don't have enough evidence from the technical manuals to answer "
                "your question confidently.\n\n"
                "Could you provide more detail — for example, the equipment name, "
                "model number, or the specific procedure you are looking for?"
            )
            yield _sse_data(insufficient_msg)
            yield _sse_event(
                "citations",
                CitationsPayload(citations=[]).model_dump_json(),
            )
            yield _sse_data("[DONE]")
            # Still persist the assistant's "no evidence" reply
            if is_storage_enabled():
                await chat_store.append_assistant_message(
                    thread_id, user_id, insufficient_msg, citations=[]
                )
            return

        # ── 5. Get or create Agent Framework session ──────────────────────────
        is_cold_start = thread_id not in _af_sessions
        af_session = _af_sessions.get(thread_id)

        if af_session is None:
            af_session = af_agent.create_session()
            _af_sessions[thread_id] = af_session
            logger.info(
                "AgentRuntime: new AF session created | thread=%s", thread_id
            )

            # ── 6. Hydrate cold-started session with Cosmos history ───────────
            if is_storage_enabled():
                prior_messages = await chat_store.get_messages(
                    thread_id, max_turns=COSMOS_HISTORY_MAX_TURNS
                )
                if prior_messages:
                    block = format_history_block(prior_messages)
                    history_provider.store_history_block(af_session, block)
                    logger.info(
                        "AgentRuntime: loaded %d prior turns from Cosmos | thread=%s",
                        len(prior_messages), thread_id,
                    )
        else:
            logger.info(
                "AgentRuntime: warm AF session reused | thread=%s", thread_id
            )

        # ── 7. Hand pre-retrieved results to RagContextProvider ───────────────
        rag_provider.store_results(af_session, results)

        # ── 8. GENERATE — stream via Agent Framework ChatAgent ────────────────
        last_ping_at = time.monotonic()
        answer_buf: list[str] = []
        stream_error = False
        try:
            async for update in af_agent.run(
                question, stream=True, session=af_session
            ):
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

        # ── 9. CITE — only emit citations if the agent used sources ───────────
        answer_text = "".join(answer_buf)
        used_sources = "Sources:" in answer_text or "[1]" in answer_text
        citations = build_citations(results) if used_sources else []
        yield _sse_event("citations", CitationsPayload(citations=citations).model_dump_json())
        yield _sse_data("[DONE]")

        # ── 10. Persist assistant message and citations ───────────────────────
        if is_storage_enabled() and answer_text:
            citations_dicts = [c.model_dump() for c in citations]
            status = "error" if stream_error else "complete"
            await chat_store.append_assistant_message(
                thread_id,
                user_id,
                answer_text,
                citations=citations_dicts,
                status=status,
            )
            logger.info(
                "AgentRuntime: assistant message persisted | thread=%s citations=%d status=%s",
                thread_id, len(citations_dicts), status,
            )
