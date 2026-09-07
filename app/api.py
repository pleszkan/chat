import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.discord import DiscordIdentityProvider
from app.adapters.event_broker import GenerationEventBroker
from app.adapters.jwt_codec import JWTAccessTokenCodec
from app.adapters.openrouter import OpenRouterGateway
from app.adapters.security_logging import configure_security_logger
from app.adapters.sqlalchemy_repository import (
    SqlAlchemyAuthRepository,
    SqlAlchemyConversationRepository,
    create_schema,
)
from app.application.auth_service import AuthResult, AuthService
from app.application.ports import FederatedIdentityProvider, ModelStreamGateway
from app.application.services import ChatService, ConversationNotFound, GenerationService
from app.domain.auth import Principal, User
from app.domain.conversation import Conversation, Generation, GenerationStatus
from app.domain.errors import (
    AuthenticationFailed,
    GenerationAlreadyRunning,
    ProviderAuthenticationError,
)

generation_logger = logging.getLogger("chat.generation")


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "role": user.role.value,
    }


def _auth_payload(result: AuthResult) -> dict:
    return {
        "access_token": result.access_token,
        "token_type": "Bearer",
        "expires_in": result.expires_in,
        "user": _user_payload(result.user),
    }


def _conversation_payload(conversation: Conversation) -> dict:
    return {
        "id": conversation.id,
        "owner_id": conversation.owner_id,
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat(),
        "messages": [
            {"id": m.id, "role": m.role.value, "text": m.text, "sequence": m.sequence}
            for m in conversation.messages
        ],
        "generations": [
            {
                "id": g.id,
                "status": g.status.value,
                "assistant_message_id": g.assistant_message_id,
                "error_code": g.error_code,
                "error_message": g.error_message,
            }
            for g in conversation.generations
        ],
    }


def _validated_app_origin(value: str) -> str:
    origin = value.rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("APP_ORIGIN must be an HTTPS origin without a path, query, or fragment")
    return origin


def create_app(
    database_url: str | None = None,
    gateway: ModelStreamGateway | None = None,
    model: str | None = None,
    *,
    providers: dict[str, FederatedIdentityProvider] | None = None,
    app_origin: str | None = None,
    jwt_secret: str | None = None,
) -> FastAPI:
    database_url = database_url or os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./chat.db")
    model = model or os.getenv("OPENROUTER_MODEL", "minimax/minimax-m3:free")
    app_origin = _validated_app_origin(app_origin or os.environ["APP_ORIGIN"])
    jwt_secret = jwt_secret or os.environ["JWT_SECRET"]
    if providers is None:
        discord = DiscordIdentityProvider(
            os.environ["DISCORD_CLIENT_ID"], os.environ["DISCORD_CLIENT_SECRET"]
        )
        providers = {discord.id: discord}
    engine = create_async_engine(database_url)
    conversation_repository = SqlAlchemyConversationRepository(engine)
    auth_repository = SqlAlchemyAuthRepository(engine)

    def clock() -> datetime:
        return datetime.now(UTC)

    security_logger = configure_security_logger()
    chat_service = ChatService(
        conversation_repository, clock, lambda: str(uuid4()), security_logger
    )
    generation_service = GenerationService(conversation_repository, clock)
    auth_service = AuthService(
        auth_repository,
        providers,
        JWTAccessTokenCodec(jwt_secret, "hexagonal-chat", "hexagonal-chat-browser"),
        clock,
        logger=security_logger,
    )
    gateway = gateway or OpenRouterGateway(os.environ["OPENROUTER_API_KEY"])
    event_broker = GenerationEventBroker()
    idempotency: dict[tuple[str, str], str] = {}
    background_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await create_schema(engine)
        await auth_repository.cleanup_expired(clock())
        yield
        for task in background_tasks:
            if not task.done():
                task.cancel()
        await engine.dispose()

    app = FastAPI(title="Hexagonal Chat API", lifespan=lifespan)

    def redirect_uri(provider_id: str) -> str:
        return f"{app_origin}/v1/auth/oauth/{provider_id}/callback"

    def oauth_binding_cookie(state: str) -> str:
        return f"oauth_binding_{state}"

    def unauthorized(detail: str = "Not authenticated") -> HTTPException:
        return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})

    async def current_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        if authorization is None:
            raise unauthorized()
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            raise unauthorized()
        if not token or " " in token:
            raise unauthorized("Invalid access token")
        try:
            return auth_service.authenticate_access_token(token)
        except AuthenticationFailed as error:
            raise unauthorized("Invalid access token") from error

    def require_same_origin(origin: Annotated[str | None, Header()] = None) -> None:
        if origin != app_origin:
            raise HTTPException(403, "Cross-origin request rejected")

    def invalid_refresh_response() -> Response:
        response = JSONResponse(
            {"detail": "Invalid refresh token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
        response.delete_cookie(
            "chat_refresh", path="/v1/auth", secure=True, httponly=True, samesite="strict"
        )
        return response

    async def run_generation(conversation_id: str, generation: Generation) -> None:
        try:
            conversation = await generation_service.get_conversation(conversation_id)
            messages = [
                {"role": item.role.value, "content": item.text}
                for item in conversation.messages
                if item.id != generation.assistant_message_id
            ]
            async for chunk in gateway.stream(generation.model, messages):
                await generation_service.checkpoint(
                    conversation_id, generation.assistant_message_id, chunk
                )
                event_broker.publish(generation.id, {"event": "delta", "text": chunk})
            await generation_service.complete(conversation_id, generation.id)
            event_broker.publish(generation.id, {"event": "completed"})
        except Exception:
            generation_logger.exception(
                "generation.failed",
                extra={
                    "conversation_id": conversation_id,
                    "generation_id": generation.id,
                    "model": generation.model,
                },
            )
            await generation_service.fail(
                conversation_id,
                generation.id,
                "provider_error",
                "The model provider could not complete this reply.",
            )
            event_broker.publish(
                generation.id,
                {
                    "event": "failed",
                    "code": "provider_error",
                    "message": "The model provider could not complete this reply.",
                },
            )

    @app.get("/v1/auth/providers")
    async def list_auth_providers() -> list[dict]:
        return [
            {
                "id": item.id,
                "display_name": item.display_name,
                "start_url": f"/v1/auth/oauth/{item.id}/start",
            }
            for item in auth_service.configured_providers()
        ]

    @app.get("/v1/auth/oauth/{provider_id}/start")
    async def start_oauth(provider_id: str) -> RedirectResponse:
        try:
            started = await auth_service.begin_oauth(provider_id, redirect_uri(provider_id))
        except AuthenticationFailed as error:
            raise HTTPException(404, "Authentication provider not found") from error
        response = RedirectResponse(
            started.authorization_url,
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            oauth_binding_cookie(started.state),
            started.browser_binding,
            max_age=600,
            path=f"/v1/auth/oauth/{provider_id}/callback",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/v1/auth/oauth/{provider_id}/callback")
    async def oauth_callback(
        provider_id: str,
        request: Request,
        code: str | None = None,
        state: str | None = None,
    ) -> Response:
        binding_cookie_name = oauth_binding_cookie(state) if state else None
        browser_binding = request.cookies.get(binding_cookie_name) if binding_cookie_name else None
        try:
            if not state or not browser_binding:
                raise AuthenticationFailed("OAuth sign-in failed")
            if not code or request.query_params.get("error"):
                await auth_service.reject_oauth(provider_id, state, browser_binding)
                raise AuthenticationFailed("OAuth sign-in failed")
            result = await auth_service.complete_oauth(
                provider_id,
                code,
                state,
                browser_binding,
                redirect_uri(provider_id),
            )
        except AuthenticationFailed, ProviderAuthenticationError:
            response: Response = JSONResponse({"detail": "OAuth sign-in failed"}, status_code=400)
        else:
            response = RedirectResponse("/", status_code=307)
            response.set_cookie(
                "chat_refresh",
                result.refresh_token,
                max_age=30 * 24 * 60 * 60,
                path="/v1/auth",
                secure=True,
                httponly=True,
                samesite="strict",
            )
        response.headers["Cache-Control"] = "no-store"
        if binding_cookie_name:
            response.delete_cookie(
                binding_cookie_name,
                path=f"/v1/auth/oauth/{provider_id}/callback",
                secure=True,
                httponly=True,
                samesite="lax",
            )
        return response

    @app.post("/v1/auth/refresh")
    async def refresh_access(
        _: Annotated[None, Depends(require_same_origin)],
        chat_refresh: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        if chat_refresh is None:
            return invalid_refresh_response()
        try:
            result = await auth_service.refresh(chat_refresh)
        except AuthenticationFailed:
            return invalid_refresh_response()
        response = JSONResponse(_auth_payload(result), headers={"Cache-Control": "no-store"})
        response.set_cookie(
            "chat_refresh",
            result.refresh_token,
            max_age=30 * 24 * 60 * 60,
            path="/v1/auth",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/v1/auth/logout", status_code=204)
    async def logout(
        _: Annotated[None, Depends(require_same_origin)],
        chat_refresh: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        await auth_service.logout(chat_refresh)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            "chat_refresh", path="/v1/auth", secure=True, httponly=True, samesite="strict"
        )
        return response

    @app.get("/v1/auth/me")
    async def current_user(principal: Annotated[Principal, Depends(current_principal)]) -> dict:
        try:
            return _user_payload(await auth_service.current_user(principal))
        except AuthenticationFailed as error:
            raise unauthorized() from error

    @app.post("/v1/conversations", status_code=201)
    async def create_conversation(
        request: CreateConversationRequest,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> dict:
        return _conversation_payload(
            await chat_service.create_conversation(principal, request.title)
        )

    @app.get("/v1/conversations")
    async def list_conversations(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> list[dict]:
        return [
            _conversation_payload(item) for item in await chat_service.list_conversations(principal)
        ]

    @app.get("/v1/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> dict:
        try:
            return _conversation_payload(
                await chat_service.get_conversation(principal, conversation_id)
            )
        except ConversationNotFound as error:
            raise HTTPException(404, "Conversation not found") from error

    @app.delete("/v1/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(
        conversation_id: str,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Response:
        try:
            await chat_service.delete_conversation(principal, conversation_id)
        except ConversationNotFound as error:
            raise HTTPException(404, "Conversation not found") from error
        return Response(status_code=204)

    @app.post("/v1/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(
        conversation_id: str,
        request: SendMessageRequest,
        principal: Annotated[Principal, Depends(current_principal)],
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict:
        try:
            if idempotency_key and (conversation_id, idempotency_key) in idempotency:
                await chat_service.get_conversation(principal, conversation_id)
                return {"generation_id": idempotency[(conversation_id, idempotency_key)]}
            generation = await chat_service.start_turn(
                principal, conversation_id, request.text, model
            )
        except ConversationNotFound as error:
            raise HTTPException(404, "Conversation not found") from error
        except GenerationAlreadyRunning as error:
            raise HTTPException(409, str(error)) from error
        if idempotency_key:
            idempotency[(conversation_id, idempotency_key)] = generation.id
        task = asyncio.create_task(run_generation(conversation_id, generation))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        await asyncio.sleep(0)
        return {"generation_id": generation.id, "status": generation.status.value}

    @app.get("/v1/generations/{generation_id}/events")
    async def generation_events(
        generation_id: str,
        conversation_id: str,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> StreamingResponse:
        try:
            conversation = await chat_service.get_conversation(principal, conversation_id)
        except ConversationNotFound as error:
            raise HTTPException(404, "Generation not found") from error
        generation = next(
            (item for item in conversation.generations if item.id == generation_id), None
        )
        if generation is None:
            raise HTTPException(404, "Generation not found")
        assistant = next(
            item for item in conversation.messages if item.id == generation.assistant_message_id
        )
        queue = (
            event_broker.subscribe(generation_id)
            if generation.status is GenerationStatus.RUNNING
            else None
        )
        if queue is not None:
            # Register before re-reading so a terminal publication cannot fall between
            # the snapshot read and broker registration.
            try:
                latest_conversation = await chat_service.get_conversation(
                    principal, conversation_id
                )
            except ConversationNotFound as error:
                event_broker.unsubscribe(generation_id, queue)
                raise HTTPException(404, "Generation not found") from error
            generation = next(
                (item for item in latest_conversation.generations if item.id == generation_id),
                None,
            )
            if generation is None:
                event_broker.unsubscribe(generation_id, queue)
                raise HTTPException(404, "Generation not found")
            assistant = next(
                item
                for item in latest_conversation.messages
                if item.id == generation.assistant_message_id
            )
            # Deltas published before the refreshed snapshot are already represented in it.
            terminal_event: dict | None = None
            while True:
                try:
                    pending_event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if pending_event["event"] in {"completed", "failed"}:
                    terminal_event = pending_event
            if terminal_event is not None:
                queue.put_nowait(terminal_event)

        async def events() -> AsyncIterator[str]:
            try:
                snapshot = {"text": assistant.text, "status": generation.status.value}
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
                if queue is None or generation.status is not GenerationStatus.RUNNING:
                    return
                while True:
                    event = await queue.get()
                    event_data = {key: value for key, value in event.items() if key != "event"}
                    yield f"event: {event['event']}\ndata: {json.dumps(event_data)}\n\n"
                    if event["event"] in {"completed", "failed"}:
                        return
            finally:
                if queue is not None:
                    event_broker.unsubscribe(generation_id, queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.frontend("/", directory=str(Path(__file__).parent / "static"))
    return app
