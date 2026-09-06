from collections.abc import AsyncIterator
from typing import Protocol

from app.domain.conversation import Conversation


class ConversationRepository(Protocol):
    async def save(self, conversation: Conversation) -> None: ...

    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def list(self) -> list[Conversation]: ...

    async def delete(self, conversation_id: str) -> bool: ...


class ModelStreamGateway(Protocol):
    def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...
