from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.domain.auth import FederatedProfile


class FakeGateway:
    async def stream(self, model: str, messages: list[dict[str, str]]):
        yield "Hello"


class FakeProvider:
    id = "discord"
    display_name = "Discord"

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        return "https://identity.example"

    async def authenticate(self, code: str, redirect_uri: str) -> FederatedProfile:
        return FederatedProfile("1", "Ada", None)


def test_root_serves_the_chat_frontend(tmp_path: Path):
    provider = FakeProvider()
    app = create_app(
        f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}",
        FakeGateway(),
        "demo/free-model",
        providers={provider.id: provider},
        app_origin="https://chat.example",
        jwt_secret="x" * 32,
    )

    with TestClient(app, base_url="https://chat.example") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Hexagonal Chat" in response.text
    assert 'id="message-form"' in response.text
    assert 'id="conversation-list"' in response.text
    assert 'id="new-chat"' in response.text
    assert 'id="signed-out"' in response.text
    assert 'id="auth-providers"' in response.text
    assert 'id="current-user"' in response.text
    assert 'id="logout"' in response.text
