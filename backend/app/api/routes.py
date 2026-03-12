"""FastAPI routes — thin routes delegating to AgentRuntime."""

import logging
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.agent_runtime.agent import AgentRuntime
from app.agent_runtime.session import AgentSession
from app.api.schemas import ChatRequest

logger = logging.getLogger(__name__)
router = APIRouter()

_runtime = AgentRuntime()


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream a grounded answer with citations via Server-Sent Events."""
    logger.info(
        "POST /chat/stream | session=%s | question=%s",
        request.session_id,
        request.question,
    )

    session = AgentSession(question=request.question)
    if request.session_id:
        session.session_id = request.session_id

    return StreamingResponse(
        _runtime.run_stream(request.question, session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat")
async def chat(request: ChatRequest) -> dict:
    """Return a grounded answer and citations as normal JSON."""
    logger.info(
        "POST /chat | session=%s | question=%s",
        request.session_id,
        request.question,
    )

    session = AgentSession(question=request.question)
    if request.session_id:
        session.session_id = request.session_id

    # Reuse runtime logic if available
    if hasattr(_runtime, "run"):
        result = await _runtime.run(request.question, session)

        if isinstance(result, dict):
            return {
                "answer": result.get("answer", ""),
                "citations": result.get("citations", []),
            }

        return {
            "answer": str(result),
            "citations": [],
        }

    # Fallback: consume the existing stream and reconstruct final answer/citations
    answer_parts = []
    citations = []

    async for chunk in _runtime.run_stream(request.question, session):
        if not isinstance(chunk, str):
            continue

        line = chunk.strip()

        if line.startswith("event: citations"):
            continue

        if line.startswith("data: [DONE]"):
            break

        if line.startswith("data: "):
            value = line[len("data: ") :]

            # try citations payload
            if value.startswith("{") and '"citations"' in value:
                try:
                    import json
                    payload = json.loads(value)
                    citations.extend(payload.get("citations", []))
                    continue
                except Exception:
                    pass

            # ignore keepalive
            if value == "keepalive":
                continue

            answer_parts.append(value.replace("\\n", "\n"))

    return {
        "answer": "".join(answer_parts).strip(),
        "citations": citations,
    }
