import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from app.application.ports import (
    AccessTokenCodec,
    AuthRepository,
    FederatedIdentityProvider,
)
from app.domain.auth import Principal, RefreshRotationStatus, User
from app.domain.errors import AuthenticationFailed, ProviderAuthenticationError


@dataclass(frozen=True)
class OAuthStart:
    state: str
    browser_binding: str
    authorization_url: str


@dataclass(frozen=True)
class AuthResult:
    user: User
    access_token: str
    refresh_token: str
    expires_in: int = 900


@dataclass(frozen=True)
class ProviderSummary:
    id: str
    display_name: str


class AuthService:
    access_lifetime_seconds = 900

    def __init__(
        self,
        repository: AuthRepository,
        providers: dict[str, FederatedIdentityProvider],
        token_codec: AccessTokenCodec,
        clock: Callable[[], datetime],
        ids: Callable[[], str] = lambda: str(uuid4()),
        secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.token_codec = token_codec
        self.clock = clock
        self.ids = ids
        self.secret_factory = secret_factory
        self.logger = logger or logging.getLogger("chat.security")

    def configured_providers(self) -> list[ProviderSummary]:
        return [
            ProviderSummary(provider.id, provider.display_name)
            for provider in self.providers.values()
        ]

    async def begin_oauth(self, provider_id: str, redirect_uri: str) -> OAuthStart:
        provider = self._provider(provider_id)
        now = self.clock()
        state = self.secret_factory()
        browser_binding = self.secret_factory()
        await self.repository.create_oauth_transaction(
            self._hash(state),
            self._hash(browser_binding),
            provider_id,
            now,
            now + timedelta(minutes=10),
        )
        return OAuthStart(
            state,
            browser_binding,
            provider.authorization_url(state, redirect_uri),
        )

    async def complete_oauth(
        self,
        provider_id: str,
        code: str,
        state: str,
        browser_binding: str,
        redirect_uri: str,
    ) -> AuthResult:
        provider = self._provider(provider_id)
        now = self.clock()
        valid = await self.repository.consume_oauth_transaction(
            self._hash(state), self._hash(browser_binding), provider_id, now
        )
        if not valid:
            self.logger.warning("auth.login_failed", extra={"provider": provider_id})
            raise AuthenticationFailed("OAuth sign-in failed")
        try:
            profile = await provider.authenticate(code, redirect_uri)
        except ProviderAuthenticationError:
            self.logger.warning("auth.login_failed", extra={"provider": provider_id})
            raise
        user = await self.repository.provision_identity(
            provider_id, profile, self.ids(), self.ids(), now
        )
        if not user.active:
            self.logger.warning(
                "auth.login_failed", extra={"provider": provider_id, "user_id": user.id}
            )
            raise AuthenticationFailed("OAuth sign-in failed")
        family_id = self.ids()
        refresh_secret = self.secret_factory()
        await self.repository.create_refresh_session(
            family_id,
            user.id,
            self._hash(refresh_secret),
            now,
            now + timedelta(days=30),
        )
        self.logger.info(
            "auth.login_succeeded",
            extra={"provider": provider_id, "user_id": user.id, "session_id": family_id},
        )
        return self._result(user, family_id, 0, refresh_secret, now)

    async def refresh(self, refresh_token: str) -> AuthResult:
        family_id, generation, current_secret = self._parse_refresh_token(refresh_token)
        next_secret = self.secret_factory()
        now = self.clock()
        rotation = await self.repository.rotate_refresh_token(
            family_id,
            generation,
            self._hash(current_secret),
            self._hash(next_secret),
            now,
        )
        if rotation.status is RefreshRotationStatus.REPLAYED:
            self.logger.warning("auth.refresh_replay", extra={"session_id": family_id})
            raise AuthenticationFailed("Invalid refresh token")
        if (
            rotation.status is not RefreshRotationStatus.ROTATED
            or rotation.user_id is None
            or rotation.generation is None
        ):
            raise AuthenticationFailed("Invalid refresh token")
        user = await self.repository.get_user(rotation.user_id)
        if user is None or not user.active:
            raise AuthenticationFailed("Invalid refresh token")
        return self._result(user, family_id, rotation.generation, next_secret, now)

    async def logout(self, refresh_token: str | None) -> None:
        if refresh_token is None:
            return
        try:
            family_id, generation, refresh_secret = self._parse_refresh_token(refresh_token)
        except AuthenticationFailed:
            return
        revoked = await self.repository.revoke_refresh_family(
            family_id, generation, self._hash(refresh_secret), self.clock()
        )
        if revoked:
            self.logger.info("auth.session_revoked", extra={"session_id": family_id})

    def authenticate_access_token(self, access_token: str) -> Principal:
        return self.token_codec.decode(access_token, self.clock())

    async def current_user(self, principal: Principal) -> User:
        user = await self.repository.get_user(principal.user_id)
        if user is None or not user.active:
            raise AuthenticationFailed("User is unavailable")
        return user

    def _provider(self, provider_id: str) -> FederatedIdentityProvider:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise AuthenticationFailed("OAuth sign-in failed")
        return provider

    def _result(
        self, user: User, family_id: str, generation: int, refresh_secret: str, now: datetime
    ) -> AuthResult:
        return AuthResult(
            user,
            self.token_codec.encode(user, family_id, now),
            f"{family_id}.{generation}.{refresh_secret}",
            self.access_lifetime_seconds,
        )

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _parse_refresh_token(token: str) -> tuple[str, int, str]:
        try:
            family_id, raw_generation, secret = token.split(".", 2)
            generation = int(raw_generation)
            if not family_id or generation < 0 or not secret:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise AuthenticationFailed("Invalid refresh token") from error
        return family_id, generation, secret
