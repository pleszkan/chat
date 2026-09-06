import json
from collections.abc import AsyncIterator

import httpx


class OpenRouterGateway:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key
        self.transport = transport

    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "stream": True}
        async with (
            httpx.AsyncClient(
                base_url="https://openrouter.ai", transport=self.transport, timeout=60
            ) as client,
            client.stream(
                "POST", "/api/v1/chat/completions", headers=headers, json=payload
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ")
                if data == "[DONE]":
                    return
                content = json.loads(data).get("choices", [{}])[0].get("delta", {}).get("content")
                if content:
                    yield content
