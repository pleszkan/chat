from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Self

from app.domain.errors import GenerationAlreadyRunning


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class GenerationStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Message:
    id: str
    role: MessageRole
    text: str
    sequence: int
    created_at: datetime


@dataclass
class Generation:
    id: str
    assistant_message_id: str
    model: str
    status: GenerationStatus
    started_at: datetime
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class Conversation:
    id: str
    owner_id: str
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    messages: list[Message] = field(default_factory=list)
    generations: list[Generation] = field(default_factory=list)

    @classmethod
    def create(
        cls, conversation_id: str, owner_id: str, now: datetime, title: str | None = None
    ) -> Self:
        return cls(
            id=conversation_id,
            owner_id=owner_id,
            created_at=now,
            updated_at=now,
            title=title,
        )

    @property
    def has_running_generation(self) -> bool:
        return any(generation.status is GenerationStatus.RUNNING for generation in self.generations)

    def add_user_message(self, message_id: str, text: str, now: datetime) -> Message:
        if self.has_running_generation:
            raise GenerationAlreadyRunning("A generation is already running for this conversation")
        message = Message(message_id, MessageRole.USER, text, len(self.messages), now)
        self.messages.append(message)
        self.updated_at = now
        return message

    def start_generation(
        self, generation_id: str, assistant_message_id: str, model: str, now: datetime
    ) -> Generation:
        if self.has_running_generation:
            raise GenerationAlreadyRunning("A generation is already running for this conversation")
        self.messages.append(
            Message(assistant_message_id, MessageRole.ASSISTANT, "", len(self.messages), now)
        )
        generation = Generation(
            generation_id, assistant_message_id, model, GenerationStatus.RUNNING, now
        )
        self.generations.append(generation)
        self.updated_at = now
        return generation

    def append_assistant_text(self, message_id: str, text: str, now: datetime) -> None:
        message = next(message for message in self.messages if message.id == message_id)
        if message.role is not MessageRole.ASSISTANT:
            raise ValueError("Only assistant messages may receive streamed text")
        message.text += text
        self.updated_at = now

    def complete_generation(self, generation_id: str, now: datetime) -> None:
        generation = self._generation(generation_id)
        generation.status = GenerationStatus.COMPLETED
        generation.completed_at = now
        self.updated_at = now

    def fail_generation(self, generation_id: str, code: str, message: str, now: datetime) -> None:
        generation = self._generation(generation_id)
        generation.status = GenerationStatus.FAILED
        generation.error_code = code
        generation.error_message = message
        generation.completed_at = now
        self.updated_at = now

    def _generation(self, generation_id: str) -> Generation:
        return next(generation for generation in self.generations if generation.id == generation_id)
