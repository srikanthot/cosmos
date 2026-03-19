"""Microsoft Agent Framework singleton — AzureOpenAIChatClient + Agent.

Reads configuration from environment variables (loaded by settings.py at
import time via python-dotenv):

  AZURE_OPENAI_ENDPOINT            – Azure OpenAI resource URL
  AZURE_OPENAI_API_KEY             – API key
  AZURE_OPENAI_API_VERSION         – e.g. 2024-06-01
  AZURE_OPENAI_CHAT_DEPLOYMENT_NAME – chat model deployment name

The module exposes singletons used by AgentRuntime:
  af_agent         – the configured Agent instance
  rag_provider     – the RagContextProvider instance
  history_provider – the CosmosHistoryProvider instance (cold-start history injection)
"""

from agent_framework import InMemoryHistoryProvider
from agent_framework.azure import AzureOpenAIChatClient

from app.agent_runtime.af_rag_context_provider import RagContextProvider
from app.agent_runtime.history_context_provider import CosmosHistoryProvider
from app.agent_runtime.prompts import SYSTEM_PROMPT

# Shared provider instances — AgentRuntime calls their store_* methods
# before agent.run() so providers can inject context in before_run().
rag_provider = RagContextProvider()
history_provider = CosmosHistoryProvider()

# AzureOpenAIChatClient reads AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
# AZURE_OPENAI_API_VERSION, and AZURE_OPENAI_CHAT_DEPLOYMENT_NAME from env.
_client = AzureOpenAIChatClient()

af_agent = _client.as_agent(
    name="PSEGTechManualAgent",
    instructions=SYSTEM_PROMPT,
    context_providers=[
        # InMemoryHistoryProvider maintains multi-turn conversation memory
        # in session.state for the lifetime of the process.
        InMemoryHistoryProvider(),
        # CosmosHistoryProvider injects prior conversation history from Cosmos DB
        # on the first call of a cold-started (post-restart) session.
        history_provider,
        # RagContextProvider injects the pre-retrieved Azure AI Search chunks
        # as additional instructions before every LLM call.
        rag_provider,
    ],
)
