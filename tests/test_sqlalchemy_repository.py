import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.sqlalchemy_repository import (
    SqlAlchemyAuthRepository,
    SqlAlchemyConversationRepository,
    create_schema,
)
from app.domain.auth import FederatedProfile, RefreshRotationStatus, UserRole
from app.domain.conversation import Conversation, GenerationStatus


@pytest.mark.asyncio
async def test_repository_round_trips_ordered_messages_and_partial_generation():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyConversationRepository(engine)
    auth_repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    await auth_repository.provision_identity(
        "discord", FederatedProfile("owner-1", "Owner", None), "owner-1", "identity-1", now
    )
    conversation = Conversation.create("conversation-1", "owner-1", now, "A chat")
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


@pytest.mark.asyncio
async def test_conversation_repository_filters_by_owner():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyConversationRepository(engine)
    auth_repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    await auth_repository.provision_identity(
        "discord", FederatedProfile("owner-1", "First", None), "owner-1", "identity-1", now
    )
    await auth_repository.provision_identity(
        "discord", FederatedProfile("owner-2", "Second", None), "owner-2", "identity-2", now
    )
    await repository.save(Conversation.create("first", "owner-1", now))
    await repository.save(Conversation.create("second", "owner-2", now))

    assert [item.id for item in await repository.list("owner-1")] == ["first"]
    assert {item.id for item in await repository.list()} == {"first", "second"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_owner_must_reference_a_local_user():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyConversationRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)

    with pytest.raises(IntegrityError):
        await repository.save(Conversation.create("conversation-1", "missing-user", now))

    await engine.dispose()


@pytest.mark.asyncio
async def test_external_identity_provisioning_reuses_user_and_updates_profile():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)

    first = await repository.provision_identity(
        "discord",
        FederatedProfile("123", "Ada", "https://cdn.example/one.png"),
        "user-1",
        "identity-1",
        now,
    )
    second = await repository.provision_identity(
        "discord",
        FederatedProfile("123", "Ada Lovelace", None),
        "unused-user",
        "unused-identity",
        now,
    )

    assert first.id == second.id == "user-1"
    assert second.display_name == "Ada Lovelace"
    assert second.role is UserRole.USER
    assert second.avatar_url is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identity_provisioning_returns_the_single_local_user(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    await create_schema(engine)
    repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    profile = FederatedProfile("123", "Ada", None)

    users = await asyncio.gather(
        repository.provision_identity("discord", profile, "user-1", "identity-1", now),
        repository.provision_identity("discord", profile, "user-2", "identity-2", now),
    )

    assert users[0].id == users[1].id
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT COUNT(*) FROM users")) == 1
        assert await connection.scalar(text("SELECT COUNT(*) FROM external_identities")) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_oauth_transaction_is_bound_expires_and_can_only_be_consumed_once():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    expires = datetime(2026, 9, 4, 0, 10, tzinfo=UTC)
    await repository.create_oauth_transaction("state", "binding", "discord", now, expires)

    assert not await repository.consume_oauth_transaction("state", "wrong", "discord", now)
    assert await repository.consume_oauth_transaction("state", "binding", "discord", now)
    assert not await repository.consume_oauth_transaction("state", "binding", "discord", now)
    await repository.create_oauth_transaction("expired", "binding", "discord", now, expires)
    assert not await repository.consume_oauth_transaction(
        "expired", "binding", "discord", datetime(2026, 9, 4, 0, 11, tzinfo=UTC)
    )
    await repository.create_oauth_transaction("boundary", "binding", "discord", now, expires)
    assert not await repository.consume_oauth_transaction("boundary", "binding", "discord", expires)
    await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_rotation_replay_revokes_the_session_family():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    expires = datetime(2026, 10, 4, tzinfo=UTC)
    await repository.provision_identity(
        "discord", FederatedProfile("123", "Ada", None), "user-1", "identity-1", now
    )
    await repository.create_refresh_session("family-1", "user-1", "hash-0", now, expires)

    rotated = await repository.rotate_refresh_token("family-1", 0, "hash-0", "hash-1", now)
    replay = await repository.rotate_refresh_token("family-1", 0, "hash-0", "ignored", now)
    after_replay = await repository.rotate_refresh_token("family-1", 1, "hash-1", "ignored", now)

    assert rotated.status is RefreshRotationStatus.ROTATED
    assert rotated.user_id == "user-1"
    assert replay.status is RefreshRotationStatus.REPLAYED
    assert after_replay.status is RefreshRotationStatus.INVALID
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_refresh_of_one_generation_revokes_the_family(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'refresh.db'}")
    await create_schema(engine)
    repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    expires = datetime(2026, 10, 4, tzinfo=UTC)
    await repository.provision_identity(
        "discord", FederatedProfile("123", "Ada", None), "user-1", "identity-1", now
    )
    await repository.create_refresh_session("family-1", "user-1", "hash-0", now, expires)

    results = await asyncio.gather(
        repository.rotate_refresh_token("family-1", 0, "hash-0", "hash-a", now),
        repository.rotate_refresh_token("family-1", 0, "hash-0", "hash-b", now),
    )

    assert {result.status for result in results} == {
        RefreshRotationStatus.ROTATED,
        RefreshRotationStatus.REPLAYED,
    }
    assert (
        await repository.rotate_refresh_token("family-1", 1, "hash-a", "unused", now)
    ).status is RefreshRotationStatus.INVALID
    assert (
        await repository.rotate_refresh_token("family-1", 1, "hash-b", "unused", now)
    ).status is RefreshRotationStatus.INVALID
    await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_removes_expired_oauth_and_refresh_records():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    expired = datetime(2026, 9, 3, tzinfo=UTC)
    await repository.provision_identity(
        "discord", FederatedProfile("123", "Ada", None), "user-1", "identity-1", now
    )
    await repository.create_oauth_transaction("state", "binding", "discord", expired, expired)
    await repository.create_refresh_session("family-1", "user-1", "hash", expired, expired)

    await repository.cleanup_expired(now)

    async with engine.connect() as connection:
        oauth_count = await connection.scalar(text("SELECT COUNT(*) FROM oauth_transactions"))
        family_count = await connection.scalar(
            text("SELECT COUNT(*) FROM refresh_session_families")
        )
        token_count = await connection.scalar(
            text("SELECT COUNT(*) FROM refresh_token_generations")
        )
    assert (oauth_count, family_count, token_count) == (0, 0, 0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_creating_session_opportunistically_cleans_expired_families():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    repository = SqlAlchemyAuthRepository(engine)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    await repository.provision_identity(
        "discord", FederatedProfile("123", "Ada", None), "user-1", "identity-1", now
    )
    await repository.create_refresh_session(
        "expired-family", "user-1", "old-hash", now, datetime(2026, 9, 3, tzinfo=UTC)
    )

    await repository.create_refresh_session(
        "active-family", "user-1", "new-hash", now, datetime(2026, 10, 4, tzinfo=UTC)
    )

    async with engine.connect() as connection:
        family_ids = set(
            (await connection.execute(text("SELECT id FROM refresh_session_families"))).scalars()
        )
        token_family_ids = set(
            (
                await connection.execute(text("SELECT family_id FROM refresh_token_generations"))
            ).scalars()
        )
    assert family_ids == {"active-family"}
    assert token_family_ids == {"active-family"}
    await engine.dispose()
