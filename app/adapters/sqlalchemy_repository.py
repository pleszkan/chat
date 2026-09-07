import hmac
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    event,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.auth import (
    FederatedProfile,
    RefreshRotation,
    RefreshRotationStatus,
    User,
    UserRole,
)
from app.domain.conversation import Conversation, Generation, GenerationStatus, Message, MessageRole


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
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


class UserRow(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExternalIdentityRow(Base):
    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthTransactionRow(Base):
    __tablename__ = "oauth_transactions"
    state_hash: Mapped[str] = mapped_column(String, primary_key=True)
    binding_hash: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RefreshSessionFamilyRow(Base):
    __tablename__ = "refresh_session_families"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshTokenGenerationRow(Base):
    __tablename__ = "refresh_token_generations"
    __table_args__ = (UniqueConstraint("family_id", "generation"),)
    family_id: Mapped[str] = mapped_column(
        ForeignKey("refresh_session_families.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def create_schema(engine: AsyncEngine) -> None:
    if engine.url.get_backend_name() == "sqlite" and not event.contains(
        engine.sync_engine, "connect", _enable_sqlite_foreign_keys
    ):
        event.listen(engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
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
                    owner_id=conversation.owner_id,
                    title=conversation.title,
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                )
            )
            await session.flush()
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
            await session.flush()
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
                owner_id=row.owner_id,
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

    async def list(self, owner_id: str | None) -> list[Conversation]:
        async with self.sessions() as session:
            query = select(ConversationRow.id)
            if owner_id is not None:
                query = query.where(ConversationRow.owner_id == owner_id)
            ids = (await session.scalars(query.order_by(ConversationRow.updated_at.desc()))).all()
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


class SqlAlchemyAuthRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def provision_identity(
        self,
        provider: str,
        profile: FederatedProfile,
        user_id: str,
        identity_id: str,
        now: datetime,
    ) -> User:
        try:
            async with self.sessions.begin() as session:
                identity = await self._identity(session, provider, profile.subject)
                if identity is None:
                    user = UserRow(
                        id=user_id,
                        display_name=profile.display_name,
                        avatar_url=profile.avatar_url,
                        role=UserRole.USER.value,
                        active=True,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(user)
                    await session.flush()
                    session.add(
                        ExternalIdentityRow(
                            id=identity_id,
                            user_id=user_id,
                            provider=provider,
                            subject=profile.subject,
                            created_at=now,
                        )
                    )
                    await session.flush()
                else:
                    user = await self._update_identity_user(session, identity, profile, now)
        except IntegrityError:
            async with self.sessions.begin() as session:
                identity = await self._identity(session, provider, profile.subject)
                if identity is None:
                    raise
                user = await self._update_identity_user(session, identity, profile, now)
        return self._user(user)

    @staticmethod
    async def _identity(session, provider: str, subject: str) -> ExternalIdentityRow | None:
        return await session.scalar(
            select(ExternalIdentityRow).where(
                ExternalIdentityRow.provider == provider,
                ExternalIdentityRow.subject == subject,
            )
        )

    @staticmethod
    async def _update_identity_user(
        session, identity: ExternalIdentityRow, profile: FederatedProfile, now: datetime
    ) -> UserRow:
        user = await session.get(UserRow, identity.user_id)
        if user is None:
            raise RuntimeError("External identity refers to a missing user")
        user.display_name = profile.display_name
        user.avatar_url = profile.avatar_url
        user.updated_at = now
        return user

    async def get_user(self, user_id: str) -> User | None:
        async with self.sessions() as session:
            row = await session.get(UserRow, user_id)
            return self._user(row) if row is not None else None

    async def create_oauth_transaction(
        self,
        state_hash: str,
        binding_hash: str,
        provider: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                delete(OAuthTransactionRow).where(OAuthTransactionRow.expires_at <= now)
            )
            session.add(
                OAuthTransactionRow(
                    state_hash=state_hash,
                    binding_hash=binding_hash,
                    provider=provider,
                    created_at=now,
                    expires_at=expires_at,
                )
            )

    async def consume_oauth_transaction(
        self, state_hash: str, binding_hash: str, provider: str, now: datetime
    ) -> bool:
        async with self.sessions.begin() as session:
            result = await session.execute(
                delete(OAuthTransactionRow).where(
                    OAuthTransactionRow.state_hash == state_hash,
                    OAuthTransactionRow.binding_hash == binding_hash,
                    OAuthTransactionRow.provider == provider,
                    OAuthTransactionRow.expires_at > now,
                )
            )
            await session.execute(
                delete(OAuthTransactionRow).where(OAuthTransactionRow.expires_at <= now)
            )
            return result.rowcount == 1

    async def create_refresh_session(
        self,
        family_id: str,
        user_id: str,
        secret_hash: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        async with self.sessions.begin() as session:
            await self._cleanup_expired(session, now)
            session.add(
                RefreshSessionFamilyRow(
                    id=family_id,
                    user_id=user_id,
                    created_at=now,
                    expires_at=expires_at,
                    revoked_at=None,
                )
            )
            await session.flush()
            session.add(
                RefreshTokenGenerationRow(
                    family_id=family_id,
                    generation=0,
                    secret_hash=secret_hash,
                    created_at=now,
                    consumed_at=None,
                )
            )

    async def rotate_refresh_token(
        self,
        family_id: str,
        generation: int,
        secret_hash: str,
        next_secret_hash: str,
        now: datetime,
    ) -> RefreshRotation:
        async with self.sessions.begin() as session:
            locked = await session.execute(
                update(RefreshSessionFamilyRow)
                .where(
                    RefreshSessionFamilyRow.id == family_id,
                    RefreshSessionFamilyRow.revoked_at.is_(None),
                    RefreshSessionFamilyRow.expires_at > now,
                )
                .values(id=family_id)
            )
            if locked.rowcount != 1:
                return RefreshRotation(RefreshRotationStatus.INVALID)
            family = await session.get(RefreshSessionFamilyRow, family_id)
            if family is None:
                return RefreshRotation(RefreshRotationStatus.INVALID)
            token = await session.get(
                RefreshTokenGenerationRow,
                {"family_id": family_id, "generation": generation},
            )
            if token is None or not hmac.compare_digest(token.secret_hash, secret_hash):
                return RefreshRotation(RefreshRotationStatus.INVALID)
            if token.consumed_at is not None:
                family.revoked_at = now
                return RefreshRotation(RefreshRotationStatus.REPLAYED)
            claimed = await session.execute(
                update(RefreshTokenGenerationRow)
                .where(
                    RefreshTokenGenerationRow.family_id == family_id,
                    RefreshTokenGenerationRow.generation == generation,
                    RefreshTokenGenerationRow.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
            if claimed.rowcount != 1:
                family.revoked_at = now
                return RefreshRotation(RefreshRotationStatus.REPLAYED)
            next_generation = generation + 1
            session.add(
                RefreshTokenGenerationRow(
                    family_id=family_id,
                    generation=next_generation,
                    secret_hash=next_secret_hash,
                    created_at=now,
                    consumed_at=None,
                )
            )
            return RefreshRotation(RefreshRotationStatus.ROTATED, family.user_id, next_generation)

    async def revoke_refresh_family(
        self, family_id: str, generation: int, secret_hash: str, now: datetime
    ) -> bool:
        async with self.sessions.begin() as session:
            token = await session.get(
                RefreshTokenGenerationRow,
                {"family_id": family_id, "generation": generation},
            )
            if token is None or not hmac.compare_digest(token.secret_hash, secret_hash):
                return False
            result = await session.execute(
                update(RefreshSessionFamilyRow)
                .where(
                    RefreshSessionFamilyRow.id == family_id,
                    RefreshSessionFamilyRow.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            return result.rowcount == 1

    async def cleanup_expired(self, now: datetime) -> None:
        async with self.sessions.begin() as session:
            await self._cleanup_expired(session, now)

    @staticmethod
    async def _cleanup_expired(session: AsyncSession, now: datetime) -> None:
        expired_families = select(RefreshSessionFamilyRow.id).where(
            RefreshSessionFamilyRow.expires_at <= now
        )
        await session.execute(
            delete(RefreshTokenGenerationRow).where(
                RefreshTokenGenerationRow.family_id.in_(expired_families)
            )
        )
        await session.execute(
            delete(RefreshSessionFamilyRow).where(RefreshSessionFamilyRow.expires_at <= now)
        )
        await session.execute(
            delete(OAuthTransactionRow).where(OAuthTransactionRow.expires_at <= now)
        )

    @staticmethod
    def _user(row: UserRow) -> User:
        return User(
            id=row.id,
            display_name=row.display_name,
            avatar_url=row.avatar_url,
            role=UserRole(row.role),
            active=row.active,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
