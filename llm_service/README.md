# llm-service

Production-grade, **async-first** Python SDK: one unified API for OpenAI, Anthropic, Gemini, Azure OpenAI, Ollama, vLLM, OpenRouter, Hugging Face, local stubs, and custom providers.

---

## Install

```bash
pip install llm-service
```

Optional dependency groups:

| Extra | Purpose |
|-------|---------|
| `openai`, `anthropic`, `gemini`, `azure`, `huggingface` | Official SDKs where applicable (HTTP adapters work without them) |
| `redis` | Redis cache / conversation memory |
| `cache` | Extra cache backends |
| `observability` | OpenTelemetry + Prometheus metrics |
| `templates` | Jinja2 `PromptTemplate` |
| `semantic-cache` | Semantic cache hooks (stub / advanced) |
| `cli` | Reserved (Typer/Rich ship with the base package) |

```bash
pip install "llm-service[redis,observability,templates]"
pip install -e ".[all,dev]"   # from a clone
```

### Vendoring (drop into any repo)

Copy the whole **`llm_service` directory** (this folder: `pyproject.toml`, `README.md`, `.env.example`, `tests/`, and the `llm_service` package) into your project. Then install from inside it:

```bash
cd path/to/llm_service
pip install -e ".[dev]"
```

Configure credentials via `.env` next to `pyproject.toml` (see `.env.example`).

---

## Quick start

```python
from llm_service import LLMClient

client = LLMClient(provider="mock", model="mock-model")
response = client.chat(messages=[{"role": "user", "content": "Hello"}])
print(response.content, response.usage)
```

OpenAI (set `OPENAI_API_KEY` in `.env` or the environment):

```python
with LLMClient(provider="openai", model="gpt-4o-mini") as client:
    print(client.chat([{"role": "user", "content": "Hi"}]).content)
```

---

## `LLMClient`: sync, async, lifecycle

**Async chat** (preferred inside apps that already use `asyncio`):

```python
import asyncio
from llm_service import LLMClient

async def main():
    client = LLMClient(provider="openai", model="gpt-4o-mini")
    r = await client.achat(
        [{"role": "user", "content": "Explain asyncio in one line."}],
        temperature=0.3,
        max_tokens=128,
        correlation_id="my-trace-id",
    )
    print(r.content, r.latency_ms, r.correlation_id)

asyncio.run(main())
```

**Sync `chat()`** — reuses an internal event loop and HTTP keep-alive across calls on the same client:

```python
client = LLMClient(provider="openai", model="gpt-4o-mini")
r1 = client.chat([{"role": "user", "content": "Hi"}])
r2 = client.chat([{"role": "user", "content": "Again"}])
client.close()
```

**Context manager** (closes loop + HTTP pools):

```python
with LLMClient(provider="openai", model="gpt-4o-mini") as client:
    print(client.chat([{"role": "user", "content": "Hello"}]).content)
```

**Inject a provider instance** (routing, tests, custom adapters):

```python
from llm_service import LLMClient
from llm_service.providers.registry import create_provider
from llm_service.config import LibrarySettings

settings = LibrarySettings()
provider = create_provider("ollama", settings)
client = LLMClient(provider=provider, model="llama3")
```

---

## Streaming and time-to-first-token

`astream()` talks to the provider’s streaming API (middleware chain is **not** applied to streams).

```python
import asyncio
from llm_service import LLMClient

async def main():
    with LLMClient(provider="openai", model="gpt-4o-mini") as client:
        await client.awarmup()  # optional: TLS + pool before first token
        async for chunk in client.astream([{"role": "user", "content": "Count to 5."}]):
            print(chunk.delta, end="", flush=True)
        print()

asyncio.run(main())
```

**Sync pre-connect** (when you only use `chat()`, not inside a running loop):

```python
client = LLMClient(provider="openai", model="gpt-4o-mini")
client.warm()
print(client.chat([{"role": "user", "content": "Hi"}]).content)
client.close()
```

**Stream handler + callbacks** (`llm_service.streaming`):

```python
import asyncio
from llm_service import LLMClient
from llm_service.streaming import StreamHandler

async def main():
    client = LLMClient(provider="mock", model="mock-model")

    async def on_done(text: str) -> None:
        print(f"\n[complete: {len(text)} chars]")

    handler = StreamHandler(callbacks=[])
    async for chunk in handler.handle(client.astream([{"role": "user", "content": "Hi"}]), on_complete=on_done):
        print(chunk.delta, end="")

asyncio.run(main())
```

---

## Configuration and secrets

**Environment / `.env`** (library loads `.env` from the working directory; provider keys use their own prefixes):

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `HUGGINGFACE_API_KEY`, `AZURE_OPENAI_API_KEY`, …
- `LLM_DEFAULT_PROVIDER`, `LLM_DEFAULT_MODEL`, `OPENAI_MAX_RETRIES`, `LLM_HTTP2`, etc.

**Typed settings** (`llm_service.config`):

```python
from pydantic import SecretStr
from llm_service import LLMClient
from llm_service.config import LibrarySettings, OpenAIProviderConfig

settings = LibrarySettings(
    openai=OpenAIProviderConfig(api_key=SecretStr("sk-...")),
    default_model="gpt-4o-mini",
)
client = LLMClient(provider="openai", settings=settings, model="gpt-4o-mini")
```

**YAML / JSON file** via `LibrarySettings` / `LLM_CONFIG_FILE`:

```python
from llm_service.config import LibrarySettings

settings = LibrarySettings.from_file("llm_config.yaml")
```

**Per-client override** with `config=`:

```python
from llm_service import LLMClient
from llm_service.config import OpenAIProviderConfig

client = LLMClient(
    provider="openai",
    model="gpt-4o-mini",
    config=OpenAIProviderConfig(base_url="https://api.openai.com/v1", timeout=30.0),
)
```

---

## Built-in providers

Register names (use as `LLMClient(provider="...")`):

`openai`, `anthropic`, `gemini`, `azure_openai` / `azure`, `ollama`, `vllm`, `openrouter`, `huggingface` / `hf`, `local`, `mock`

**List and factory:**

```python
from llm_service import create_provider, list_providers
from llm_service.config import LibrarySettings

print(list_providers())
p = create_provider("openai", LibrarySettings())
```

**Swap provider without changing call sites:**

```python
for name, model in [("openai", "gpt-4o-mini"), ("ollama", "llama3"), ("mock", "mock-model")]:
    with LLMClient(provider=name, model=model) as c:
        print(name, "->", c.chat([{"role": "user", "content": "2+2?"}]).content)
```

---

## Custom provider (`@register`)

```python
from collections.abc import AsyncIterator

from llm_service.core import BaseLLMProvider, LLMRequest, LLMResponse, StreamChunk, TokenUsage
from llm_service.providers.registry import register

@register("my_backend")
class MyBackend(BaseLLMProvider):
    @property
    def provider_name(self) -> str:
        return "my_backend"

    async def achat(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            provider=self.provider_name,
            model=request.model,
            content="…",
            usage=TokenUsage(),
            correlation_id=request.correlation_id,
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="Hello ", correlation_id=request.correlation_id)
        yield StreamChunk(delta="world", finish_reason="stop", correlation_id=request.correlation_id)
```

Import your module once so the decorator runs, then `LLMClient(provider="my_backend", model="any", config=...)`.

---

## Multi-provider routing and fallback

```python
from llm_service import LLMClient
from llm_service.config import LibrarySettings, MockProviderConfig
from llm_service.providers.registry import create_provider
from llm_service.routing import LLMRouter, FallbackStrategy

settings = LibrarySettings()
primary = create_provider("mock", settings, config=MockProviderConfig(extra={"responses": ["from primary"]}))
fallback = create_provider("mock", settings, config=MockProviderConfig(extra={"responses": ["from fallback"]}))
router = LLMRouter([primary, fallback], strategy=FallbackStrategy())

client = LLMClient(provider=router, model="mock-model")
print(client.chat([{"role": "user", "content": "Hi"}]).content)
```

**Strategies** (`llm_service.routing`): `RoundRobinStrategy`, `CostOptimizedStrategy`, `FallbackStrategy`, `LowestLatencyStrategy`.

**Load helpers**: `WeightedRoundRobin`, `HealthAwareBalancer`.

---

## Middleware pipeline

Non-streaming `achat` / `chat` run: **Logging → Metrics** by default. Pass `middlewares=` to replace the chain.

```python
from llm_service import LLMClient
from llm_service.middleware import (
    LoggingMiddleware,
    MetricsMiddleware,
    CachingMiddleware,
    RateLimitMiddleware,
    TracingMiddleware,
    PromptGuardMiddleware,
)
from llm_service.cache import InMemoryResponseCache

cache = InMemoryResponseCache(default_ttl_seconds=300)
client = LLMClient(
    provider="mock",
    model="mock-model",
    middlewares=[
        LoggingMiddleware(),
        CachingMiddleware(cache),
        RateLimitMiddleware(rate=20.0, per_seconds=1.0),
        TracingMiddleware(),
        MetricsMiddleware(),
    ],
)
```

**Prompt guard** — async validators receive only `MiddlewareContext`:

```python
from llm_service.middleware import PromptGuardMiddleware
from llm_service.middleware.base import MiddlewareContext

async def no_eggs(ctx: MiddlewareContext) -> None:
    text = str(ctx.request.messages[-1].content)
    if "egg" in text.lower():
        raise ValueError("blocked")

guard = PromptGuardMiddleware([no_eggs])
# pass guard in middlewares=[..., guard, ...]
```

---

## Core types and tool calling

**Typed messages** (`llm_service.core`):

```python
from llm_service.core import Message, Role, LLMRequest

req = LLMRequest(
    messages=[
        Message(role=Role.SYSTEM, content="You are concise."),
        Message(role=Role.USER, content="Hi"),
    ],
    model="gpt-4o-mini",
)
```

**Tools** (`llm_service.tools`):

```python
import asyncio
from llm_service import LLMClient
from llm_service.tools import Tool, ToolExecutor

async def get_city(name: str) -> str:
    return f"{name} is a city."

executor = ToolExecutor({"get_city": get_city})
tools = [
    Tool(
        name="get_city",
        description="City lookup",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
]

async def main():
    client = LLMClient(provider="openai", model="gpt-4o-mini")
    r = await client.achat([{"role": "user", "content": "What is Paris?"}], tools=[t.model_dump() for t in tools])
    # If the model returns tool_calls, dispatch with executor.execute(...)

asyncio.run(main())
```

---

## Prompt templates (optional extra)

```bash
pip install "llm-service[templates]"
```

```python
from llm_service.prompts import PromptTemplate
from llm_service import LLMClient

tpl = PromptTemplate("Summarize in {{ lang }}: {{ text }}")
messages = tpl.to_messages(system="Be brief.", lang="French", text="Long article …")
with LLMClient(provider="openai", model="gpt-4o-mini") as client:
    print(client.chat(messages).content)
```

---

## Conversation memory

```python
import asyncio
from llm_service.memory import InMemoryConversationMemory
from llm_service.core import Message, Role

async def main():
    mem = InMemoryConversationMemory(max_messages=20)
    await mem.append(Message(role=Role.USER, content="Hi"))
    history = await mem.get()
    print(history)

asyncio.run(main())
```

`RedisConversationMemory` is available with the `redis` extra.

---

## Caching responses

```python
from llm_service.cache import InMemoryResponseCache, RedisResponseCache

# Exact-match cache (used with CachingMiddleware)
mem = InMemoryResponseCache(default_ttl_seconds=600)
# RedisResponseCache(url="redis://localhost:6379/0")  # requires [redis]
```

---

## Transport, retry, circuit breaker

```python
from llm_service.transport import build_async_client, CircuitBreaker, run_with_retry

# Low-level: pooled httpx client, optional HTTP/2 via LLM_HTTP2=1 + pip install "httpx[h2]"
client = build_async_client(timeout=30.0)

# Circuit breaker + retry helpers for custom integrations
breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0, provider="openai")
```

---

## Observability

```python
from llm_service.observability import configure_logging, configure_tracing
from llm_service.observability.metrics import LLM_REQUESTS  # Prometheus counters when extra installed

configure_logging(level="INFO", fmt="json")
# configure_tracing(endpoint="http://localhost:4317")  # requires [observability]
```

---

## Exceptions

All inherit from `LLMServiceError` (`llm_service.core`):

`ProviderError`, `AuthenticationError`, `RateLimitError`, `InvalidRequestError`, `ContextLengthExceededError`, `TimeoutError`, `ConnectionError`, `StreamingError`, `RetryExhaustedError`, `CircuitOpenError`, `CacheError`, `ConfigurationError`, `MiddlewareError`, `ImportExtraError`

```python
from llm_service import LLMClient
from llm_service.core import AuthenticationError, RateLimitError

try:
    LLMClient(provider="openai", model="gpt-4o-mini").chat([{"role": "user", "content": "Hi"}])
except AuthenticationError as e:
    print(e.provider, e.status_code)
```

---

## Testing helpers

```python
from llm_service.testing import MockLLMProvider, message_user, usage_fixture
from llm_service.config import MockProviderConfig

fake = MockLLMProvider(MockProviderConfig(extra={"responses": ["test"]}))
```

---

## CLI

```bash
llm-service providers
llm-service config-validate path/to/config.yaml
llm-service version
```

---

## Performance checklist

- Reuse **one** `LLMClient` (or `with` block); sync `chat()` keeps a **single loop** and **HTTP keep-alive**.
- **`await awarmup()`** / **`warm()`** before streaming to reduce **TTFT**.
- Prefer **smaller models** (e.g. `gpt-4o-mini`) when latency matters.
- **`OPENAI_MAX_RETRIES=3`** only if you need retries; default is tuned for low latency.
- **`LLM_HTTP2=1`** + `pip install "httpx[h2]"` for optional HTTP/2.

---

## Development

Run these from the **`llm_service` project directory** (where `pyproject.toml` lives):

```bash
pip install -e ".[all,dev]"
pytest
ruff check llm_service tests
mypy llm_service
```

## License

MIT
