import httpx
import pytest

from app.adapters.openrouter import OpenRouterGateway


@pytest.mark.asyncio
async def test_gateway_yields_text_deltas_from_openrouter_sse():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    gateway = OpenRouterGateway("secret", transport=httpx.MockTransport(handler))
    chunks = [
        chunk
        async for chunk in gateway.stream("demo/free-model", [{"role": "user", "content": "Hi"}])
    ]

    assert chunks == ["Hello", " world"]
