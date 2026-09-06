from collections.abc import AsyncIterator
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.api import create_app


class FakeGateway:
    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        yield "Hello"
        yield " there"


class FailingGateway:
    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        yield "Partial"
        raise RuntimeError("upstream unavailable")


def test_message_submission_returns_accepted_and_persists_streamed_reply(tmp_path: Path):
    app = create_app(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}", FakeGateway(), "demo/free-model")

    with TestClient(app) as client:
        conversation = client.post("/v1/conversations", json={"title": "Demo"}).json()
        accepted = client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"text": "Hi"},
            headers={"Idempotency-Key": "request-1"},
        )
        deadline = time.monotonic() + 1
        while True:
            transcript = client.get(f"/v1/conversations/{conversation['id']}").json()
            if transcript["generations"][0]["status"] == "completed" or time.monotonic() >= deadline:
                break
            time.sleep(0.01)

    assert accepted.status_code == 202
    assert accepted.json()["generation_id"]
    assert [message["text"] for message in transcript["messages"]] == ["Hi", "Hello there"]


def test_provider_failure_retains_partial_assistant_output(tmp_path: Path):
    app = create_app(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}", FailingGateway(), "demo/free-model")

    with TestClient(app) as client:
        conversation_id = client.post("/v1/conversations", json={}).json()["id"]
        client.post(f"/v1/conversations/{conversation_id}/messages", json={"text": "Hi"})
        deadline = time.monotonic() + 1
        while True:
            transcript = client.get(f"/v1/conversations/{conversation_id}").json()
            if transcript["generations"][0]["status"] == "failed" or time.monotonic() >= deadline:
                break
            time.sleep(0.01)

    assert transcript["messages"][-1]["text"] == "Partial"
    assert transcript["generations"][0]["error_code"] == "provider_error"
