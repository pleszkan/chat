from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest

from app.adapters.discord import DiscordIdentityProvider
from app.adapters.jwt_codec import JWTAccessTokenCodec
from app.domain.auth import User, UserRole
from app.domain.errors import AuthenticationFailed, ProviderAuthenticationError


@pytest.mark.asyncio
async def test_discord_exchanges_form_encoded_code_and_maps_current_user():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v10/oauth2/token":
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["authorization_code"],
                "code": ["oauth-code"],
                "redirect_uri": ["https://chat.example/v1/auth/oauth/discord/callback"],
                "client_id": ["client-id"],
                "client_secret": ["client-secret"],
            }
            return httpx.Response(
                200, json={"access_token": "discord-token", "token_type": "Bearer"}
            )
        assert request.url.path == "/api/v10/users/@me"
        assert request.headers["authorization"] == "Bearer discord-token"
        return httpx.Response(
            200,
            json={
                "id": "123",
                "username": "ada",
                "global_name": "Ada Lovelace",
                "avatar": "avatar-hash",
            },
        )

    provider = DiscordIdentityProvider(
        "client-id", "client-secret", transport=httpx.MockTransport(handler)
    )
    profile = await provider.authenticate(
        "oauth-code", "https://chat.example/v1/auth/oauth/discord/callback"
    )

    assert profile.subject == "123"
    assert profile.display_name == "Ada Lovelace"
    assert profile.avatar_url == "https://cdn.discordapp.com/avatars/123/avatar-hash.png"
    assert len(requests) == 2


def test_discord_authorization_url_requests_only_identify_scope():
    provider = DiscordIdentityProvider("client-id", "client-secret")

    query = parse_qs(
        urlparse(
            provider.authorization_url(
                "state-value", "https://chat.example/v1/auth/oauth/discord/callback"
            )
        ).query
    )

    assert query == {
        "response_type": ["code"],
        "client_id": ["client-id"],
        "scope": ["identify"],
        "state": ["state-value"],
        "redirect_uri": ["https://chat.example/v1/auth/oauth/discord/callback"],
    }


@pytest.mark.asyncio
async def test_discord_rejects_malformed_token_response():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer"})

    provider = DiscordIdentityProvider(
        "client-id", "client-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderAuthenticationError):
        await provider.authenticate("code", "https://chat.example/callback")


@pytest.mark.asyncio
async def test_discord_rejects_non_object_token_response():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not-an-object"])

    provider = DiscordIdentityProvider(
        "client-id", "client-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderAuthenticationError):
        await provider.authenticate("code", "https://chat.example/callback")


@pytest.mark.asyncio
async def test_discord_translates_timeouts_to_provider_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = DiscordIdentityProvider(
        "client-id", "client-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderAuthenticationError):
        await provider.authenticate("code", "https://chat.example/callback")


@pytest.mark.asyncio
async def test_discord_rejects_profile_without_subject():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"username": "ada", "global_name": None, "avatar": None})

    provider = DiscordIdentityProvider(
        "client-id", "client-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderAuthenticationError):
        await provider.authenticate("code", "https://chat.example/callback")


def test_jwt_codec_round_trips_required_claims():
    now = datetime.now(UTC)
    user = User("user-1", "Ada", None, UserRole.ADMIN, True, now, now)
    codec = JWTAccessTokenCodec(
        "x" * 32, "hexagonal-chat", "hexagonal-chat-browser", jti_factory=lambda: "jti-1"
    )

    token = codec.encode(user, "session-1", now)
    principal = codec.decode(token, now)
    claims = jwt.decode(
        token,
        "x" * 32,
        algorithms=["HS256"],
        issuer="hexagonal-chat",
        audience="hexagonal-chat-browser",
    )

    assert principal.user_id == "user-1"
    assert principal.role is UserRole.ADMIN
    assert principal.session_id == "session-1"
    assert {
        "iss",
        "aud",
        "sub",
        "role",
        "sid",
        "iat",
        "exp",
        "jti",
    } <= claims.keys()
    assert claims["exp"] - claims["iat"] == 900


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda secret, now: jwt.encode(
            {
                "iss": "hexagonal-chat",
                "aud": "hexagonal-chat-browser",
                "sub": "user-1",
                "role": "user",
                "sid": "session-1",
                "iat": now,
                "exp": now + timedelta(minutes=15),
                "jti": "jti-1",
            },
            "wrong-secret-that-is-at-least-32-bytes",
            algorithm="HS256",
        ),
        lambda secret, now: jwt.encode(
            {
                "iss": "hexagonal-chat",
                "aud": "hexagonal-chat-browser",
                "sub": "user-1",
                "role": "user",
                "sid": "session-1",
                "iat": now,
                "exp": now + timedelta(minutes=15),
                "jti": "jti-1",
            },
            secret,
            algorithm="HS384",
        ),
        lambda secret, now: jwt.encode(
            {
                "iss": "wrong-issuer",
                "aud": "hexagonal-chat-browser",
                "sub": "user-1",
                "role": "user",
                "sid": "session-1",
                "iat": now,
                "exp": now + timedelta(minutes=15),
                "jti": "jti-1",
            },
            secret,
            algorithm="HS256",
        ),
        lambda secret, now: jwt.encode(
            {
                "iss": "hexagonal-chat",
                "aud": "wrong-audience",
                "sub": "user-1",
                "role": "user",
                "sid": "session-1",
                "iat": now,
                "exp": now + timedelta(minutes=15),
                "jti": "jti-1",
            },
            secret,
            algorithm="HS256",
        ),
        lambda secret, now: jwt.encode(
            {
                "iss": "hexagonal-chat",
                "aud": "hexagonal-chat-browser",
                "sub": "user-1",
                "role": "bogus",
                "sid": "session-1",
                "iat": now,
                "exp": now + timedelta(minutes=15),
                "jti": "jti-1",
            },
            secret,
            algorithm="HS256",
        ),
        lambda secret, now: jwt.encode(
            {
                "iss": "hexagonal-chat",
                "aud": "hexagonal-chat-browser",
                "sub": "user-1",
                "role": "user",
                "sid": "session-1",
                "iat": now,
                "exp": now + timedelta(minutes=15),
            },
            secret,
            algorithm="HS256",
        ),
    ],
)
def test_jwt_codec_rejects_invalid_tokens(token_factory):
    now = datetime.now(UTC)
    secret = "x" * 64
    codec = JWTAccessTokenCodec(secret, "hexagonal-chat", "hexagonal-chat-browser")

    with pytest.raises(AuthenticationFailed):
        codec.decode(token_factory(secret, now), now)


def test_jwt_codec_rejects_expired_token():
    now = datetime.now(UTC)
    secret = "x" * 64
    token = jwt.encode(
        {
            "iss": "hexagonal-chat",
            "aud": "hexagonal-chat-browser",
            "sub": "user-1",
            "role": "user",
            "sid": "session-1",
            "iat": now - timedelta(minutes=30),
            "exp": now - timedelta(minutes=15),
            "jti": "jti-1",
        },
        secret,
        algorithm="HS256",
    )
    codec = JWTAccessTokenCodec(secret, "hexagonal-chat", "hexagonal-chat-browser")

    with pytest.raises(AuthenticationFailed):
        codec.decode(token, now)


def test_jwt_codec_uses_injected_time_for_temporal_validation():
    issued_at = datetime(2040, 1, 1, tzinfo=UTC)
    user = User("user-1", "Ada", None, UserRole.USER, True, issued_at, issued_at)
    codec = JWTAccessTokenCodec("x" * 64, "hexagonal-chat", "hexagonal-chat-browser")
    token = codec.encode(user, "session-1", issued_at)

    assert codec.decode(token, issued_at).user_id == "user-1"
    with pytest.raises(AuthenticationFailed):
        codec.decode(token, issued_at + timedelta(minutes=16))


def test_jwt_codec_rejects_non_finite_temporal_claims():
    now = datetime.now(UTC)
    secret = "x" * 64
    codec = JWTAccessTokenCodec(secret, "hexagonal-chat", "hexagonal-chat-browser")
    claims = {
        "iss": "hexagonal-chat",
        "aud": "hexagonal-chat-browser",
        "sub": "user-1",
        "role": "user",
        "sid": "session-1",
        "jti": "jti-1",
    }

    for issued_at, expires_at in (
        (float("nan"), now.timestamp() + 900),
        (now.timestamp() - 1, float("nan")),
        (float("-inf"), now.timestamp() + 900),
        (now.timestamp() - 1, float("inf")),
    ):
        token = jwt.encode(
            {**claims, "iat": issued_at, "exp": expires_at}, secret, algorithm="HS256"
        )
        with pytest.raises(AuthenticationFailed):
            codec.decode(token, now)
