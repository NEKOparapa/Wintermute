"""Keep upstream credential echoes out of errors saved by the runner and its workers."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from openai import APIError, AsyncOpenAI, AsyncStream

from wintermute.agent.core.errors import ModelProtocolError
from wintermute.logging.formatters import Redactor


@asynccontextmanager
async def model_stream[T](
    client: AsyncOpenAI,
    key_provider: Callable[[], str] | None,
    create: Callable[[dict[str, str] | None], Awaitable[AsyncStream[T]]],
) -> AsyncIterator[AsyncStream[T]]:
    key = key_provider() if key_provider else getattr(client, "api_key", None)
    headers = {"Authorization": f"Bearer {key}"} if key_provider else None
    try:
        stream = await create(headers)
        try:
            yield stream
        finally:
            await stream.close()
    except (APIError, ModelProtocolError) as error:
        # SDK errors stringify their response body, sometimes as a Python dict repr.
        # Keep the exception type and status so callers can still distinguish e.g. HTTP 401.
        if isinstance(key, str) and key:
            original = str(error)
            sanitized = Redactor((key,)).known_secrets(original)
            if sanitized != original:
                error.args = (sanitized,)
                if isinstance(error, APIError):
                    error.message = sanitized
        raise
