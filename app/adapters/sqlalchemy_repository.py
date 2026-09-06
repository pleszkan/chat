from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.conversation import Conversation, Generation, GenerationStatus, Message, MessageRole


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MessageRow(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GenerationRow(Base):
    __tablename__ = "generations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    assistant_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    model: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


class SqlAlchemyConversationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def save(self, conversation: Conversation) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                delete(GenerationRow).where(GenerationRow.conversation_id == conversation.id)
            )
            await session.execute(
                delete(MessageRow).where(MessageRow.conversation_id == conversation.id)
            )
            await session.execute(
                delete(ConversationRow).where(ConversationRow.id == conversation.id)
            )
            session.add(
                ConversationRow(
                    id=conversation.id,
                    title=conversation.title,
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                )
            )
            session.add_all(
                MessageRow(
                    id=item.id,
                    conversation_id=conversation.id,
                    role=item.role.value,
                    text=item.text,
                    sequence=item.sequence,
                    created_at=item.created_at,
                )
                for item in conversation.messages
            )
            session.add_all(
                GenerationRow(
                    id=item.id,
                    conversation_id=conversation.id,
                    assistant_message_id=item.assistant_message_id,
                    model=item.model,
                    status=item.status.value,
                    started_at=item.started_at,
                    completed_at=item.completed_at,
                    error_code=item.error_code,
                    error_message=item.error_message,
                )
                for item in conversation.generations
            )

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self.sessions() as session:
            row = await session.get(ConversationRow, conversation_id)
            if row is None:
                return None
            messages = (
                await session.scalars(
                    select(MessageRow)
                    .where(MessageRow.conversation_id == conversation_id)
                    .order_by(MessageRow.sequence)
                )
            ).all()
            generations = (
                await session.scalars(
                    select(GenerationRow)
                    .where(GenerationRow.conversation_id == conversation_id)
                    .order_by(GenerationRow.started_at)
                )
            ).all()
            return Conversation(
                id=row.id,
                title=row.title,
                created_at=row.created_at,
                updated_at=row.updated_at,
                messages=[
                    Message(
                        item.id, MessageRole(item.role), item.text, item.sequence, item.created_at
                    )
                    for item in messages
                ],
                generations=[
                    Generation(
                        item.id,
                        item.assistant_message_id,
                        item.model,
                        GenerationStatus(item.status),
                        item.started_at,
                        item.completed_at,
                        item.error_code,
                        item.error_message,
                    )
                    for item in generations
                ],
            )

    async def list(self) -> list[Conversation]:
        async with self.sessions() as session:
            ids = (
                await session.scalars(
                    select(ConversationRow.id).order_by(ConversationRow.updated_at.desc())
                )
            ).all()
        return [
            conversation
            for conversation_id in ids
            if (conversation := await self.get(conversation_id)) is not None
        ]

    async def delete(self, conversation_id: str) -> bool:
        async with self.sessions.begin() as session:
            await session.execute(
                delete(GenerationRow).where(GenerationRow.conversation_id == conversation_id)
            )
            await session.execute(
                delete(MessageRow).where(MessageRow.conversation_id == conversation_id)
            )
            result = await session.execute(
                delete(ConversationRow).where(ConversationRow.id == conversation_id)
            )
            return result.rowcount == 1
