import hashlib
import logging
from datetime import UTC, datetime

import pytest

from app.application.auth_service import AuthService, ProviderSummary
from app.domain.auth import (
    FederatedProfile,
    Principal,
    RefreshRotation,
    RefreshRotationStatus,
    User,
    UserRole,
)
from app.domain.errors import AuthenticationFailed, ProviderAuthenticationError


class FakeProvider:
    id = "discord"
    display_name = "Discord"

    def __init__(self) -> None:
        self.codes: list[str] = []

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        return f"https://discord.example/authorize?state={state}&redirect_uri={redirect_uri}"

    async def authenticate(self, code: str, redirect_uri: str) -> FederatedProfile:
        self.codes.append(code)
        return FederatedProfile("discord-123", "Ada", None)


class FakeCodec:
    def encode(self, user: User, session_id: str, now: datetime) -> str:
        return f"access:{user.id}:{session_id}:{user.role.value}"

    def decode(self, token: str, now: datetime) -> Principal:
        _, user_id, session_id, role = token.split(":")
        return Principal(user_id, "", None, UserRole(role), session_id)


class MemoryAuthRepository:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.transactions: dict[str, tuple[str, str]] = {}
        self.users: dict[str, User] = {}
        self.created_session: tuple[str, str, str] | None = None
        self.rotation = RefreshRotation(RefreshRotationStatus.INVALID)

    async def create_oauth_transaction(
        self, state_hash, binding_hash, provider, now, expires_at
    ) -> None:
        self.transactions[state_hash] = (binding_hash, provider)

    async def consume_oauth_transaction(self, state_hash, binding_hash, provider, now) -> bool:
        expected = self.transactions.pop(state_hash, None)
        return expected == (binding_hash, provider)

    async def provision_identity(self, provider, profile, user_id, identity_id, now) -> User:
        user = next(iter(self.users.values()), None)
        if user is None:
            user = User(
                user_id, profile.display_name, profile.avatar_url, UserRole.USER, True, now, now
            )
            self.users[user.id] = user
        return user

    async def create_refresh_session(
        self, family_id, user_id, secret_hash, now, expires_at
    ) -> None:
        self.created_session = (family_id, user_id, secret_hash)

    async def rotate_refresh_token(
        self, family_id, generation, secret_hash, next_secret_hash, now
    ) -> RefreshRotation:
        self.rotation_args = (family_id, generation, secret_hash, next_secret_hash)
        return self.rotation

    async def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    async def revoke_refresh_family(self, family_id, generation, secret_hash, now) -> bool:
        self.revoked = (family_id, generation, secret_hash)
        return True


def test_provider_metadata_does_not_depend_on_http_routes():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    provider = FakeProvider()
    service = AuthService(
        MemoryAuthRepository(now),
        {provider.id: provider},
        FakeCodec(),
        lambda: now,
    )

    assert service.configured_providers() == [ProviderSummary("discord", "Discord")]


@pytest.mark.asyncio
async def test_oauth_login_hashes_secrets_provisions_user_and_issues_session():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    repository = MemoryAuthRepository(now)
    provider = FakeProvider()
    secrets = iter(["state-secret", "binding-secret", "refresh-secret"])
    ids = iter(["user-1", "identity-1", "family-1"])
    service = AuthService(
        repository,
        {"discord": provider},
        FakeCodec(),
        lambda: now,
        ids.__next__,
        secrets.__next__,
    )

    start = await service.begin_oauth("discord", "https://chat.example/callback")
    result = await service.complete_oauth(
        "discord",
        "oauth-code",
        start.state,
        start.browser_binding,
        "https://chat.example/callback",
    )

    assert start.state == "state-secret"
    assert "state=state-secret" in start.authorization_url
    assert hashlib.sha256(b"state-secret").hexdigest() not in start.authorization_url
    assert provider.codes == ["oauth-code"]
    assert result.user.id == "user-1"
    assert result.access_token == "access:user-1:family-1:user"
    assert result.refresh_token == "family-1.0.refresh-secret"
    assert result.expires_in == 900
    assert repository.created_session == (
        "family-1",
        "user-1",
        hashlib.sha256(b"refresh-secret").hexdigest(),
    )


@pytest.mark.asyncio
async def test_invalid_oauth_transaction_never_reaches_provider():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    repository = MemoryAuthRepository(now)
    provider = FakeProvider()
    service = AuthService(
        repository,
        {"discord": provider},
        FakeCodec(),
        lambda: now,
        lambda: "id",
        lambda: "secret",
    )

    with pytest.raises(AuthenticationFailed):
        await service.complete_oauth(
            "discord", "must-not-be-used", "bad-state", "bad-binding", "https://callback"
        )

    assert provider.codes == []


@pytest.mark.asyncio
async def test_provider_failure_is_security_logged_without_the_oauth_code(caplog):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    repository = MemoryAuthRepository(now)

    class FailingProvider(FakeProvider):
        async def authenticate(self, code: str, redirect_uri: str) -> FederatedProfile:
            raise ProviderAuthenticationError("provider unavailable")

    service = AuthService(
        repository,
        {"discord": FailingProvider()},
        FakeCodec(),
        lambda: now,
        lambda: "id",
        iter(["state", "binding"]).__next__,
    )
    started = await service.begin_oauth("discord", "https://callback")

    with caplog.at_level("WARNING", logger="test.security"):
        service.logger = logging.getLogger("test.security")
        with pytest.raises(ProviderAuthenticationError):
            await service.complete_oauth(
                "discord",
                "sensitive-code",
                started.state,
                started.browser_binding,
                "https://callback",
            )

    assert [record.message for record in caplog.records] == ["auth.login_failed"]
    assert caplog.records[0].provider == "discord"
    assert "sensitive-code" not in caplog.text


@pytest.mark.asyncio
async def test_refresh_rotates_secret_and_issues_access_token_for_active_user():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    repository = MemoryAuthRepository(now)
    repository.users["user-1"] = User("user-1", "Ada", None, UserRole.USER, True, now, now)
    repository.rotation = RefreshRotation(RefreshRotationStatus.ROTATED, "user-1", 1)
    service = AuthService(
        repository,
        {"discord": FakeProvider()},
        FakeCodec(),
        lambda: now,
        lambda: "unused",
        lambda: "next-secret",
    )

    result = await service.refresh("family-1.0.current-secret")

    assert result.refresh_token == "family-1.1.next-secret"
    assert result.access_token == "access:user-1:family-1:user"
    assert repository.rotation_args == (
        "family-1",
        0,
        hashlib.sha256(b"current-secret").hexdigest(),
        hashlib.sha256(b"next-secret").hexdigest(),
    )
