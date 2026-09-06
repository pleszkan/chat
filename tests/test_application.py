from datetime import UTC, datetime

import pytest

from app.application.services import ChatService
from app.domain.conversation import Conversation, GenerationStatus


class MemoryRepository:
    def __init__(self):
        self.conversations: dict[str, Conversation] = {}

    async def save(self, conversation: Conversation) -> None:
        self.conversations[conversation.id] = conversation

    async def get(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)


@pytest.mark.asyncio
async def test_start_turn_persists_user_message_and_running_generation_before_streaming():
    repository = MemoryRepository()
    now = datetime(2026, 9, 4, tzinfo=UTC)
    conversation = Conversation.create("conversation-1", now)
    await repository.save(conversation)
    service = ChatService(repository, lambda: now, ids=iter(["user-1", "generation-1", "assistant-1"]).__next__)

    generation = await service.start_turn("conversation-1", "Hello", "demo/free-model")

    saved = await repository.get("conversation-1")
    assert generation.status is GenerationStatus.RUNNING
    assert [(message.role, message.text) for message in saved.messages] == [("user", "Hello"), ("assistant", "")]


@pytest.mark.asyncio
async def test_failed_generation_keeps_checkpointed_assistant_text():
    repository = MemoryRepository()
    now = datetime(2026, 9, 4, tzinfo=UTC)
    conversation = Conversation.create("conversation-1", now)
    conversation.add_user_message("user-1", "Hello", now)
    conversation.start_generation("generation-1", "assistant-1", "demo/free-model", now)
    await repository.save(conversation)
    service = ChatService(repository, lambda: now, ids=lambda: "unused")

    await service.checkpoint("conversation-1", "assistant-1", "Partial")
    await service.fail("conversation-1", "generation-1", "provider_error", "Provider unavailable")

    saved = await repository.get("conversation-1")
    assert saved.messages[-1].text == "Partial"
    assert saved.generations[-1].status is GenerationStatus.FAILED
