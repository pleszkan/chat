import logging
from collections.abc import Callable
from datetime import datetime

from app.application.ports import ConversationRepository
from app.domain.auth import Principal
from app.domain.conversation import Conversation, Generation


class ConversationNotFound(Exception):
    pass


class ChatService:
    def __init__(
        self,
        repository: ConversationRepository,
        clock: Callable[[], datetime],
        ids: Callable[[], str],
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.ids = ids
        self.logger = logger or logging.getLogger("chat.security")

    async def create_conversation(
        self, principal: Principal, title: str | None = None
    ) -> Conversation:
        conversation = Conversation.create(self.ids(), principal.user_id, self.clock(), title)
        await self.repository.save(conversation)
        return conversation

    async def start_turn(
        self, principal: Principal, conversation_id: str, text: str, model: str
    ) -> Generation:
        conversation = await self._conversation(principal, conversation_id)
        conversation.add_user_message(self.ids(), text, self.clock())
        generation = conversation.start_generation(self.ids(), self.ids(), model, self.clock())
        await self.repository.save(conversation)
        return generation

    async def get_conversation(self, principal: Principal, conversation_id: str) -> Conversation:
        return await self._conversation(principal, conversation_id)

    async def list_conversations(self, principal: Principal) -> list[Conversation]:
        return await self.repository.list(None if principal.is_admin else principal.user_id)

    async def delete_conversation(self, principal: Principal, conversation_id: str) -> bool:
        await self._conversation(principal, conversation_id)
        return await self.repository.delete(conversation_id)

    async def _conversation(self, principal: Principal, conversation_id: str) -> Conversation:
        conversation = await self.repository.get(conversation_id)
        if conversation is None or (
            conversation.owner_id != principal.user_id and not principal.is_admin
        ):
            if conversation is not None:
                self.logger.warning(
                    "auth.access_denied",
                    extra={
                        "user_id": principal.user_id,
                        "conversation_id": conversation_id,
                    },
                )
            raise ConversationNotFound(conversation_id)
        return conversation


class GenerationService:
    """Trusted generation-worker updates that are never exposed directly over HTTP."""

    def __init__(self, repository: ConversationRepository, clock: Callable[[], datetime]) -> None:
        self.repository = repository
        self.clock = clock

    async def checkpoint(self, conversation_id: str, assistant_message_id: str, text: str) -> None:
        conversation = await self._conversation(conversation_id)
        conversation.append_assistant_text(assistant_message_id, text, self.clock())
        await self.repository.save(conversation)

    async def complete(self, conversation_id: str, generation_id: str) -> None:
        conversation = await self._conversation(conversation_id)
        conversation.complete_generation(generation_id, self.clock())
        await self.repository.save(conversation)

    async def fail(self, conversation_id: str, generation_id: str, code: str, message: str) -> None:
        conversation = await self._conversation(conversation_id)
        conversation.fail_generation(generation_id, code, message, self.clock())
        await self.repository.save(conversation)

    async def get_conversation(self, conversation_id: str) -> Conversation:
        return await self._conversation(conversation_id)

    async def _conversation(self, conversation_id: str) -> Conversation:
        conversation = await self.repository.get(conversation_id)
        if conversation is None:
            raise ConversationNotFound(conversation_id)
        return conversation
