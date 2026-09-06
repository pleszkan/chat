from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app


class FakeGateway:
    async def stream(self, model: str, messages: list[dict[str, str]]):
        yield "Hello"


def test_root_serves_the_chat_frontend(tmp_path: Path):
    app = create_app(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}", FakeGateway(), "demo/free-model")

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Hexagonal Chat" in response.text
    assert 'id="message-form"' in response.text
    assert 'id="conversation-list"' in response.text
    assert 'id="new-chat"' in response.text
