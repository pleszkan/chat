from datetime import UTC, datetime

import pytest

from app.domain.conversation import Conversation, GenerationStatus, MessageRole
from app.domain.errors import GenerationAlreadyRunning


def test_conversation_rejects_a_second_user_turn_while_generation_is_running():
    conversation = Conversation.create("conversation-1", datetime(2026, 9, 4, tzinfo=UTC))
    conversation.add_user_message("message-1", "Hello", datetime(2026, 9, 4, tzinfo=UTC))
    conversation.start_generation(
        "generation-1", "message-2", "demo/free-model", datetime(2026, 9, 4, tzinfo=UTC)
    )

    with pytest.raises(GenerationAlreadyRunning):
        conversation.add_user_message("message-3", "Again", datetime(2026, 9, 4, tzinfo=UTC))


def test_generation_checkpoints_partial_text_and_completes():
    now = datetime(2026, 9, 4, tzinfo=UTC)
    conversation = Conversation.create("conversation-1", now)
    conversation.add_user_message("message-1", "Hello", now)
    generation = conversation.start_generation("generation-1", "message-2", "demo/free-model", now)

    conversation.append_assistant_text("message-2", "Hi", now)
    conversation.append_assistant_text("message-2", " there", now)
    conversation.complete_generation("generation-1", now)

    assert generation.status is GenerationStatus.COMPLETED
    assert [(message.role, message.text) for message in conversation.messages] == [
        (MessageRole.USER, "Hello"),
        (MessageRole.ASSISTANT, "Hi there"),
    ]
