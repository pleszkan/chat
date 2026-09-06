from datetime import UTC, datetime

import pytest

from app.application.services import ChatService, ConversationNotFound, GenerationService
from app.domain.auth import Principal, UserRole
from app.domain.conversation import Conversation, GenerationStatus


class MemoryRepository:
    def __init__(self):
        self.conversations: dict[str, Conversation] = {}

    async def save(self, conversation: Conversation) -> None:
        self.conversations[conversation.id] = conversation

    async def get(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)

    async def list(self, owner_id: str | None = None) -> list[Conversation]:
        return [
            item
            for item in self.conversations.values()
            if owner_id is None or item.owner_id == owner_id
        ]

    async def delete(self, conversation_id: str) -> bool:
        return self.conversations.pop(conversation_id, None) is not None


USER = Principal("user-1", "User", None, UserRole.USER, "session-1")
OTHER = Principal("user-2", "Other", None, UserRole.USER, "session-2")
ADMIN = Principal("admin-1", "Admin", None, UserRole.ADMIN, "session-3")


@pytest.mark.asyncio
async def test_start_turn_persists_user_message_and_running_generation_before_streaming():
    repository = MemoryRepository()
    now = datetime(2026, 9, 4, tzinfo=UTC)
    conversation = Conversation.create("conversation-1", USER.user_id, now)
    await repository.save(conversation)
    service = ChatService(
        repository, lambda: now, ids=iter(["user-1", "generation-1", "assistant-1"]).__next__
    )

    generation = await service.start_turn(USER, "conversation-1", "Hello", "demo/free-model")

    saved = await repository.get("conversation-1")
    assert generation.status is GenerationStatus.RUNNING
    assert [(message.role, message.text) for message in saved.messages] == [
        ("user", "Hello"),
        ("assistant", ""),
    ]


@pytest.mark.asyncio
async def test_failed_generation_keeps_checkpointed_assistant_text():
    repository = MemoryRepository()
    now = datetime(2026, 9, 4, tzinfo=UTC)
    conversation = Conversation.create("conversation-1", USER.user_id, now)
    conversation.add_user_message("user-1", "Hello", now)
    conversation.start_generation("generation-1", "assistant-1", "demo/free-model", now)
    await repository.save(conversation)
    service = GenerationService(repository, lambda: now)

    await service.checkpoint("conversation-1", "assistant-1", "Partial")
    await service.fail("conversation-1", "generation-1", "provider_error", "Provider unavailable")

    saved = await repository.get("conversation-1")
    assert saved.messages[-1].text == "Partial"
    assert saved.generations[-1].status is GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_regular_user_cannot_discover_another_users_conversation():
    repository = MemoryRepository()
    now = datetime(2026, 9, 4, tzinfo=UTC)
    await repository.save(Conversation.create("conversation-1", USER.user_id, now))
    service = ChatService(repository, lambda: now, ids=lambda: "unused")

    with pytest.raises(ConversationNotFound):
        await service.get_conversation(OTHER, "conversation-1")

    assert await service.list_conversations(OTHER) == []


@pytest.mark.asyncio
async def test_admin_can_read_and_list_all_conversations():
    repository = MemoryRepository()
    now = datetime(2026, 9, 4, tzinfo=UTC)
    conversation = Conversation.create("conversation-1", USER.user_id, now)
    await repository.save(conversation)
    service = ChatService(repository, lambda: now, ids=lambda: "unused")

    assert await service.get_conversation(ADMIN, conversation.id) is conversation
    assert await service.list_conversations(ADMIN) == [conversation]
