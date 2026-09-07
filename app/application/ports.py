from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from app.domain.auth import (
    FederatedProfile,
    Principal,
    RefreshRotation,
    User,
)
from app.domain.conversation import Conversation


class ConversationRepository(Protocol):
    async def save(self, conversation: Conversation) -> None: ...

    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def list(self, owner_id: str | None) -> list[Conversation]: ...

    async def delete(self, conversation_id: str) -> bool: ...


class ModelStreamGateway(Protocol):
    def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...


class FederatedIdentityProvider(Protocol):
    id: str
    display_name: str

    def authorization_url(self, state: str, redirect_uri: str) -> str: ...

    async def authenticate(self, code: str, redirect_uri: str) -> FederatedProfile: ...


class UserIdentityRepository(Protocol):
    async def provision_identity(
        self,
        provider: str,
        profile: FederatedProfile,
        user_id: str,
        identity_id: str,
        now: datetime,
    ) -> User: ...

    async def get_user(self, user_id: str) -> User | None: ...


class OAuthTransactionRepository(Protocol):
    async def create_oauth_transaction(
        self,
        state_hash: str,
        binding_hash: str,
        provider: str,
        now: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def consume_oauth_transaction(
        self, state_hash: str, binding_hash: str, provider: str, now: datetime
    ) -> bool: ...


class RefreshSessionRepository(Protocol):
    async def create_refresh_session(
        self,
        family_id: str,
        user_id: str,
        secret_hash: str,
        now: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def rotate_refresh_token(
        self,
        family_id: str,
        generation: int,
        secret_hash: str,
        next_secret_hash: str,
        now: datetime,
    ) -> RefreshRotation: ...

    async def revoke_refresh_family(
        self, family_id: str, generation: int, secret_hash: str, now: datetime
    ) -> bool: ...

    async def cleanup_expired(self, now: datetime) -> None: ...


class AuthRepository(
    UserIdentityRepository, OAuthTransactionRepository, RefreshSessionRepository, Protocol
):
    pass


class AccessTokenCodec(Protocol):
    def encode(self, user: User, session_id: str, now: datetime) -> str: ...

    def decode(self, token: str, now: datetime) -> Principal: ...
