"""CosmosHistoryProvider — Agent Framework ContextProvider for cold-start history.

When the backend restarts, the in-process InMemoryHistoryProvider is empty even
though prior conversation turns exist in Cosmos DB.  This provider bridges the gap:

  1. AgentRuntime loads the last N messages from Cosmos and formats them into a
     single text block, then stores that block in af_session.state under
     _HISTORY_BLOCK_KEY.

  2. On the first agent.run() call for a cold-started session, before_run() reads
     the block, injects it into the system instructions via context.extend_instructions(),
     and removes it from state so it only fires once.

  3. From the second turn onward, InMemoryHistoryProvider takes over and provides
     the growing in-process message history automatically.

Result: the LLM always has prior conversation context, whether the session is warm
(InMemoryHistoryProvider has it) or cold (we inject from Cosmos once).
"""

from __future__ import annotations

import logging
from typing import Any

from agent_framework import AgentSession, BaseContextProvider, SessionContext

logger = logging.getLogger(__name__)

# State key used to hand off the formatted history block to before_run()
_HISTORY_BLOCK_KEY = "_cosmos_history_block"


def format_history_block(messages: list) -> str:
    """Format a list of MessageRecord objects into a readable prior-history block.

    The block is injected as extended system instructions so the LLM treats it
    as authoritative background context, not as a new user turn.
    """
    if not messages:
        return ""

    lines = ["--- Prior conversation history (from persistent storage) ---"]
    for msg in messages:
        role_label = "User" if msg.role == "user" else "Assistant"
        # Truncate very long messages in the context block to keep token count sane
        content = msg.content
        if len(content) > 800:
            content = content[:797] + "…"
        lines.append(f"{role_label}: {content}")
    lines.append("--- End of prior history ---")
    return "\n".join(lines)


class CosmosHistoryProvider(BaseContextProvider):
    """Injects Cosmos DB prior-conversation history on the first turn of a cold session."""

    def __init__(self) -> None:
        super().__init__("cosmos_history")

    @staticmethod
    def store_history_block(session: AgentSession, block: str) -> None:
        """Called by AgentRuntime before agent.run() to pre-load prior history.

        Only stores the block if it is non-empty — avoids a no-op extend_instructions.
        """
        if block:
            session.state[_HISTORY_BLOCK_KEY] = block

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Inject prior history block once, then remove it so InMemoryHistory takes over."""
        block: str | None = session.state.pop(_HISTORY_BLOCK_KEY, None)
        if not block:
            return

        context.extend_instructions(
            self.source_id,
            (
                "The following is the prior conversation history for this chat thread. "
                "Use it to maintain continuity when answering the current question.\n\n"
                f"{block}"
            ),
        )
        logger.info(
            "CosmosHistoryProvider: injected prior history block (%d chars) into session",
            len(block),
        )

    async def after_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        pass
