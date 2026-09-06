import asyncio
import json
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.adapters.event_broker import GenerationEventBroker
from app.api import create_app
from app.domain.auth import FederatedProfile
from app.domain.conversation import GenerationStatus
from app.domain.errors import ProviderAuthenticationError


class FakeGateway:
    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        yield "Hello"
        yield " there"


class FailingGateway:
    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        yield "Partial"
        raise RuntimeError("upstream unavailable")


class BlockingGateway:
    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        await asyncio.Future()
        yield "unreachable"


class ReplayBroker(GenerationEventBroker):
    """Replay the terminal publication that raced with the stale snapshot read."""

    def __init__(self) -> None:
        super().__init__()
        self.terminal_events: dict[str, dict] = {}

    def publish(self, generation_id: str, event: dict) -> None:
        super().publish(generation_id, event)
        if event["event"] in {"completed", "failed"}:
            self.terminal_events[generation_id] = event

    def subscribe(self, generation_id: str):
        queue = super().subscribe(generation_id)
        if event := self.terminal_events.get(generation_id):
            queue.put_nowait(event)
        return queue


class SnapshotDeltaBroker(GenerationEventBroker):
    def subscribe(self, generation_id: str):
        queue = super().subscribe(generation_id)
        queue.put_nowait({"event": "delta", "text": "already-in-snapshot"})
        queue.put_nowait({"event": "completed"})
        return queue


class FakeProvider:
    id = "discord"
    display_name = "Discord"

    def __init__(self) -> None:
        self.subject = "discord-user-1"
        self.display_name_value = "Ada"
        self.fail = False

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        return "https://identity.example/authorize?" + urlencode(
            {"state": state, "redirect_uri": redirect_uri}
        )

    async def authenticate(self, code: str, redirect_uri: str) -> FederatedProfile:
        if self.fail:
            raise ProviderAuthenticationError("sensitive provider diagnostics")
        return FederatedProfile(self.subject, self.display_name_value, None)


def make_app(tmp_path: Path, gateway=None, provider=None):
    database_path = tmp_path / "chat.db"
    provider = provider or FakeProvider()
    app = create_app(
        f"sqlite+aiosqlite:///{database_path}",
        gateway or FakeGateway(),
        "demo/free-model",
        providers={provider.id: provider},
        app_origin="https://chat.example",
        jwt_secret="x" * 32,
    )
    return app, database_path, provider


def login(client: TestClient) -> str:
    start = client.get("/v1/auth/oauth/discord/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        "/v1/auth/oauth/discord/callback",
        params={"code": "oauth-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 307
    refreshed = client.post("/v1/auth/refresh", headers={"Origin": "https://chat.example"})
    assert refreshed.status_code == 200
    return refreshed.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "origin", ["http://chat.example", "https://chat.example/path", "https://chat.example?query=1"]
)
def test_app_rejects_values_that_are_not_https_origins(tmp_path: Path, origin: str):
    provider = FakeProvider()

    with pytest.raises(ValueError, match="APP_ORIGIN"):
        create_app(
            f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}",
            FakeGateway(),
            "demo/free-model",
            providers={provider.id: provider},
            app_origin=origin,
            jwt_secret="x" * 32,
        )


def test_auth_flow_sets_secure_cookies_and_returns_current_user(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        providers = client.get("/v1/auth/providers")
        start = client.get("/v1/auth/oauth/discord/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback = client.get(
            "/v1/auth/oauth/discord/callback",
            params={"code": "oauth-code", "state": state},
            follow_redirects=False,
        )
        refreshed = client.post("/v1/auth/refresh", headers={"Origin": "https://chat.example"})
        token = refreshed.json()["access_token"]
        me = client.get("/v1/auth/me", headers=bearer(token))

    assert providers.json() == [
        {
            "id": "discord",
            "display_name": "Discord",
            "start_url": "/v1/auth/oauth/discord/start",
        }
    ]
    assert start.status_code == 307
    assert "oauth_binding_" in start.headers["set-cookie"]
    assert "HttpOnly" in start.headers["set-cookie"]
    assert "Secure" in start.headers["set-cookie"]
    assert "SameSite=lax" in start.headers["set-cookie"]
    assert "chat_refresh=" in callback.headers["set-cookie"]
    assert "SameSite=strict" in callback.headers["set-cookie"]
    assert refreshed.json()["token_type"] == "Bearer"
    assert refreshed.json()["expires_in"] == 900
    assert refreshed.headers["cache-control"] == "no-store"
    assert me.json()["display_name"] == "Ada"
    assert me.json()["role"] == "user"


def test_parallel_oauth_attempts_keep_separate_browser_bindings(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        first_start = client.get("/v1/auth/oauth/discord/start", follow_redirects=False)
        second_start = client.get("/v1/auth/oauth/discord/start", follow_redirects=False)
        first_state = parse_qs(urlparse(first_start.headers["location"]).query)["state"][0]
        second_state = parse_qs(urlparse(second_start.headers["location"]).query)["state"][0]
        first_callback = client.get(
            "/v1/auth/oauth/discord/callback",
            params={"code": "first-code", "state": first_state},
            follow_redirects=False,
        )
        second_callback = client.get(
            "/v1/auth/oauth/discord/callback",
            params={"code": "second-code", "state": second_state},
            follow_redirects=False,
        )

    assert first_callback.status_code == 307
    assert second_callback.status_code == 307


@pytest.mark.parametrize("rejection", [{"error": "access_denied"}, {}])
def test_rejected_oauth_callback_consumes_state_and_binding(
    tmp_path: Path, rejection: dict[str, str]
):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        start = client.get("/v1/auth/oauth/discord/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        cookie_name = f"oauth_binding_{state}"
        binding = client.cookies.get(cookie_name)
        rejected = client.get(
            "/v1/auth/oauth/discord/callback",
            params={"state": state, **rejection},
            follow_redirects=False,
        )
        client.cookies.set(
            cookie_name,
            binding,
            path="/v1/auth/oauth/discord/callback",
        )
        retry = client.get(
            "/v1/auth/oauth/discord/callback",
            params={"code": "oauth-code", "state": state},
            follow_redirects=False,
        )

    assert rejected.status_code == 400
    assert retry.status_code == 400


def test_oauth_provider_failure_returns_only_a_generic_error(tmp_path: Path):
    app, _, provider = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        start = client.get("/v1/auth/oauth/discord/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        provider.fail = True
        callback = client.get(
            "/v1/auth/oauth/discord/callback",
            params={"code": "sensitive-code", "state": state},
            follow_redirects=False,
        )

    assert callback.status_code == 400
    assert callback.json() == {"detail": "OAuth sign-in failed"}
    assert "sensitive" not in callback.text


def test_state_bearing_auth_responses_are_not_cacheable(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        start = client.get("/v1/auth/oauth/discord/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        callback = client.get(
            "/v1/auth/oauth/discord/callback",
            params={"code": "oauth-code", "state": state},
            follow_redirects=False,
        )
        logout = client.post("/v1/auth/logout", headers={"Origin": "https://chat.example"})

    assert start.headers["cache-control"] == "no-store"
    assert callback.headers["cache-control"] == "no-store"
    assert logout.headers["cache-control"] == "no-store"


def test_chat_endpoints_require_a_valid_bearer_token(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        missing = client.get("/v1/conversations")
        invalid = client.get("/v1/conversations", headers={"Authorization": "Bearer invalid"})

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert invalid.headers["www-authenticate"] == "Bearer"


def test_bearer_scheme_matching_is_case_insensitive(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        token = login(client)
        response = client.get("/v1/auth/me", headers={"Authorization": f"bEaReR {token}"})

    assert response.status_code == 200
    assert response.json()["display_name"] == "Ada"


def test_refresh_and_logout_reject_cross_origin_posts(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        login(client)
        refresh = client.post("/v1/auth/refresh", headers={"Origin": "https://attacker.example"})
        logout = client.post("/v1/auth/logout", headers={"Origin": "https://attacker.example"})
        missing_origin = client.post("/v1/auth/refresh")

    assert refresh.status_code == 403
    assert logout.status_code == 403
    assert missing_origin.status_code == 403


def test_refresh_replay_revokes_the_rotated_session_and_logout_clears_cookie(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        login(client)
        replayed_token = client.cookies.get("chat_refresh")
        rotated = client.post("/v1/auth/refresh", headers={"Origin": "https://chat.example"})
        current_token = client.cookies.get("chat_refresh")
        client.cookies.clear()
        client.cookies.set("chat_refresh", replayed_token, path="/v1/auth")
        replay = client.post("/v1/auth/refresh", headers={"Origin": "https://chat.example"})
        client.cookies.clear()
        client.cookies.set("chat_refresh", current_token, path="/v1/auth")
        revoked_family = client.post("/v1/auth/refresh", headers={"Origin": "https://chat.example"})

    assert rotated.status_code == 200
    assert replay.status_code == 401
    assert "Max-Age=0" in replay.headers["set-cookie"]
    assert revoked_family.status_code == 401


def test_logout_revokes_session_and_clears_refresh_cookie(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        access_token = login(client)
        refresh_token = client.cookies.get("chat_refresh")
        response = client.post("/v1/auth/logout", headers={"Origin": "https://chat.example"})
        client.cookies.set("chat_refresh", refresh_token, path="/v1/auth")
        after_logout = client.post("/v1/auth/refresh", headers={"Origin": "https://chat.example"})
        stateless_access = client.get("/v1/auth/me", headers=bearer(access_token))

    assert response.status_code == 204
    assert "chat_refresh=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert after_logout.status_code == 401
    assert stateless_access.status_code == 200


def test_regular_users_are_isolated_while_admin_can_access_all_chats(tmp_path: Path):
    app, database_path, provider = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        first_token = login(client)
        conversation = client.post(
            "/v1/conversations", json={"title": "Private"}, headers=bearer(first_token)
        ).json()
        assert conversation["owner_id"]
        generation_id = client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"text": "Owner turn"},
            headers=bearer(first_token),
        ).json()["generation_id"]
        deadline = time.monotonic() + 1
        while True:
            owner_transcript = client.get(
                f"/v1/conversations/{conversation['id']}", headers=bearer(first_token)
            ).json()
            if (
                owner_transcript["generations"][0]["status"] == "completed"
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.01)
        client.cookies.clear()
        provider.subject = "discord-user-2"
        provider.display_name_value = "Grace"
        second_token = login(client)
        assert client.get("/v1/conversations", headers=bearer(second_token)).json() == []
        denied = client.get(f"/v1/conversations/{conversation['id']}", headers=bearer(second_token))
        denied_send = client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"text": "Intrusion"},
            headers=bearer(second_token),
        )
        denied_stream = client.get(
            f"/v1/generations/{generation_id}/events",
            params={"conversation_id": conversation["id"]},
            headers=bearer(second_token),
        )
        denied_delete = client.delete(
            f"/v1/conversations/{conversation['id']}", headers=bearer(second_token)
        )
        unauthenticated_stream = client.get(
            f"/v1/generations/{generation_id}/events",
            params={"conversation_id": conversation["id"]},
        )
        with sqlite3.connect(database_path) as connection:
            connection.execute("UPDATE users SET role = 'admin' WHERE display_name = 'Grace'")
        old_token_list = client.get("/v1/conversations", headers=bearer(second_token))
        admin_token = client.post(
            "/v1/auth/refresh", headers={"Origin": "https://chat.example"}
        ).json()["access_token"]
        allowed = client.get(f"/v1/conversations/{conversation['id']}", headers=bearer(admin_token))
        admin_list = client.get("/v1/conversations", headers=bearer(admin_token))
        admin_stream = client.get(
            f"/v1/generations/{generation_id}/events",
            params={"conversation_id": conversation["id"]},
            headers=bearer(admin_token),
        )
        admin_send = client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"text": "Admin turn"},
            headers=bearer(admin_token),
        )
        deadline = time.monotonic() + 1
        while True:
            admin_transcript = client.get(
                f"/v1/conversations/{conversation['id']}", headers=bearer(admin_token)
            ).json()
            if (
                admin_transcript["generations"][-1]["status"] == "completed"
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.01)
        admin_delete = client.delete(
            f"/v1/conversations/{conversation['id']}", headers=bearer(admin_token)
        )

    assert denied.status_code == 404
    assert denied_send.status_code == 404
    assert denied_stream.status_code == 404
    assert denied_delete.status_code == 404
    assert unauthenticated_stream.status_code == 401
    assert old_token_list.json() == []
    assert allowed.status_code == 200
    assert [item["id"] for item in admin_list.json()] == [conversation["id"]]
    assert admin_stream.status_code == 200
    assert admin_send.status_code == 202
    assert admin_delete.status_code == 204


def test_message_submission_returns_accepted_and_persists_streamed_reply(tmp_path: Path):
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        token = login(client)
        conversation = client.post(
            "/v1/conversations", json={"title": "Demo"}, headers=bearer(token)
        ).json()
        accepted = client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"text": "Hi"},
            headers={**bearer(token), "Idempotency-Key": "request-1"},
        )
        deadline = time.monotonic() + 1
        while True:
            transcript = client.get(
                f"/v1/conversations/{conversation['id']}", headers=bearer(token)
            ).json()
            if (
                transcript["generations"][0]["status"] == "completed"
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.01)
        events = client.get(
            f"/v1/generations/{accepted.json()['generation_id']}/events",
            params={"conversation_id": conversation["id"]},
            headers=bearer(token),
        )
        other_conversation = client.post("/v1/conversations", json={}, headers=bearer(token)).json()
        mismatched = client.get(
            f"/v1/generations/{accepted.json()['generation_id']}/events",
            params={"conversation_id": other_conversation["id"]},
            headers=bearer(token),
        )

    assert accepted.status_code == 202
    assert [message["text"] for message in transcript["messages"]] == ["Hi", "Hello there"]
    assert "event: snapshot" in events.text
    assert mismatched.status_code == 404


def test_sse_rechecks_after_terminal_publication_races_a_stale_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(api_module, "GenerationEventBroker", ReplayBroker)
    original_get = api_module.SqlAlchemyConversationRepository.get
    stale_read = False

    async def get_with_one_stale_snapshot(repository, conversation_id: str):
        nonlocal stale_read
        conversation = await original_get(repository, conversation_id)
        if stale_read:
            stale_read = False
            conversation.generations[0].status = GenerationStatus.RUNNING
        return conversation

    monkeypatch.setattr(
        api_module.SqlAlchemyConversationRepository, "get", get_with_one_stale_snapshot
    )
    app, _, _ = make_app(tmp_path)

    with TestClient(app, base_url="https://chat.example") as client:
        token = login(client)
        conversation = client.post("/v1/conversations", json={}, headers=bearer(token)).json()
        accepted = client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"text": "Hi"},
            headers=bearer(token),
        )
        deadline = time.monotonic() + 1
        while True:
            transcript = client.get(
                f"/v1/conversations/{conversation['id']}", headers=bearer(token)
            ).json()
            if transcript["generations"][0]["status"] == "completed":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        stale_read = True
        events = client.get(
            f"/v1/generations/{accepted.json()['generation_id']}/events",
            params={"conversation_id": conversation["id"]},
            headers=bearer(token),
        )

    first_frame = events.text.split("\n\n", 1)[0]
    snapshot = json.loads(first_frame.split("data: ", 1)[1])
    assert snapshot["status"] == "completed"


def test_sse_does_not_replay_deltas_already_in_rechecked_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(api_module, "GenerationEventBroker", SnapshotDeltaBroker)
    app, _, _ = make_app(tmp_path, BlockingGateway())

    with TestClient(app, base_url="https://chat.example") as client:
        token = login(client)
        conversation = client.post("/v1/conversations", json={}, headers=bearer(token)).json()
        accepted = client.post(
            f"/v1/conversations/{conversation['id']}/messages",
            json={"text": "Hi"},
            headers=bearer(token),
        )
        events = client.get(
            f"/v1/generations/{accepted.json()['generation_id']}/events",
            params={"conversation_id": conversation["id"]},
            headers=bearer(token),
        )

    assert "event: delta" not in events.text


def test_provider_failure_retains_partial_assistant_output(tmp_path: Path):
    app, _, _ = make_app(tmp_path, FailingGateway())

    with TestClient(app, base_url="https://chat.example") as client:
        token = login(client)
        conversation_id = client.post("/v1/conversations", json={}, headers=bearer(token)).json()[
            "id"
        ]
        client.post(
            f"/v1/conversations/{conversation_id}/messages",
            json={"text": "Hi"},
            headers=bearer(token),
        )
        deadline = time.monotonic() + 1
        while True:
            transcript = client.get(
                f"/v1/conversations/{conversation_id}", headers=bearer(token)
            ).json()
            if transcript["generations"][0]["status"] == "failed" or time.monotonic() >= deadline:
                break
            time.sleep(0.01)

    assert transcript["messages"][-1]["text"] == "Partial"
    assert transcript["generations"][0]["error_code"] == "provider_error"
