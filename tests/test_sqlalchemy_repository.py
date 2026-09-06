from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.sqlalchemy_repository import SqlAlchemyConversationRepository, create_schema
from app.domain.conversation import Conversation, GenerationStatus


@pytest.mark.asyncio
async def test_repository_round_trips_ordered_messages_and_partial_generation():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyConversationRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    conversation = Conversation.create("conversation-1", now, "A chat")
    conversation.add_user_message("user-1", "Hello", now)
    generation = conversation.start_generation(
        "generation-1", "assistant-1", "demo/free-model", now
    )
    conversation.append_assistant_text("assistant-1", "Partial answer", now)
    conversation.fail_generation(generation.id, "provider_error", "Unavailable", now)

    await repository.save(conversation)
    restored = await repository.get("conversation-1")

    assert [(message.sequence, message.text) for message in restored.messages] == [
        (0, "Hello"),
        (1, "Partial answer"),
    ]
    assert restored.generations[0].status is GenerationStatus.FAILED
    assert restored.generations[0].error_code == "provider_error"
    await engine.dispose()
