import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.openrouter import OpenRouterGateway
from app.adapters.sqlalchemy_repository import SqlAlchemyConversationRepository, create_schema
from app.application.services import ChatService, ConversationNotFound
from app.domain.conversation import Conversation, Generation, GenerationStatus
from app.domain.errors import GenerationAlreadyRunning


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


def _conversation_payload(conversation: Conversation) -> dict:
    return {
        "id": conversation.id,
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


def create_app(
    database_url: str | None = None,
    gateway: OpenRouterGateway | None = None,
    model: str | None = None,
) -> FastAPI:
    database_url = database_url or os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./chat.db")
    model = model or os.getenv("OPENROUTER_MODEL", "minimax/minimax-m3:free")
    engine = create_async_engine(database_url)
    repository = SqlAlchemyConversationRepository(engine)
    service = ChatService(repository, lambda: datetime.now(UTC), lambda: str(uuid4()))
    gateway = gateway or OpenRouterGateway(os.environ["OPENROUTER_API_KEY"])
    subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
    idempotency: dict[tuple[str, str], str] = {}
    background_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await create_schema(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="Hexagonal Chat API", lifespan=lifespan)

    async def publish(generation_id: str, event: dict) -> None:
        for queue in subscribers.get(generation_id, set()):
            queue.put_nowait(event)

    async def run_generation(conversation_id: str, generation: Generation) -> None:
        try:
            conversation = await service.get_conversation(conversation_id)
            messages = [
                {"role": item.role.value, "content": item.text}
                for item in conversation.messages
                if item.id != generation.assistant_message_id
            ]
            async for chunk in gateway.stream(generation.model, messages):
                await service.checkpoint(conversation_id, generation.assistant_message_id, chunk)
                await publish(generation.id, {"event": "delta", "text": chunk})
            await service.complete(conversation_id, generation.id)
            await publish(generation.id, {"event": "completed"})
        except Exception:
            await service.fail(
                conversation_id,
                generation.id,
                "provider_error",
                "The model provider could not complete this reply.",
            )
            await publish(
                generation.id,
                {
                    "event": "failed",
                    "code": "provider_error",
                    "message": "The model provider could not complete this reply.",
                },
            )

    @app.post("/v1/conversations", status_code=201)
    async def create_conversation(request: CreateConversationRequest) -> dict:
        return _conversation_payload(await service.create_conversation(request.title))

    @app.get("/v1/conversations")
    async def list_conversations() -> list[dict]:
        return [_conversation_payload(item) for item in await service.list_conversations()]

    @app.get("/v1/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict:
        try:
            return _conversation_payload(await service.get_conversation(conversation_id))
        except ConversationNotFound as error:
            raise HTTPException(404, "Conversation not found") from error

    @app.delete("/v1/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: str) -> Response:
        if not await service.delete_conversation(conversation_id):
            raise HTTPException(404, "Conversation not found")
        return Response(status_code=204)

    @app.post("/v1/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(
        conversation_id: str,
        request: SendMessageRequest,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict:
        if idempotency_key and (conversation_id, idempotency_key) in idempotency:
            return {"generation_id": idempotency[(conversation_id, idempotency_key)]}
        try:
            generation = await service.start_turn(conversation_id, request.text, model)
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
    async def generation_events(generation_id: str, conversation_id: str) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            conversation = await service.get_conversation(conversation_id)
            generation = next(
                (item for item in conversation.generations if item.id == generation_id), None
            )
            if generation is None:
                return
            assistant = next(
                item for item in conversation.messages if item.id == generation.assistant_message_id
            )
            snapshot = {"text": assistant.text, "status": generation.status.value}
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            if generation.status is not GenerationStatus.RUNNING:
                return
            queue: asyncio.Queue[dict] = asyncio.Queue()
            subscribers.setdefault(generation_id, set()).add(queue)
            try:
                while True:
                    event = await queue.get()
                    event_data = {key: value for key, value in event.items() if key != "event"}
                    yield f"event: {event['event']}\ndata: {json.dumps(event_data)}\n\n"
                    if event["event"] in {"completed", "failed"}:
                        return
            finally:
                subscribers.get(generation_id, set()).discard(queue)

        return StreamingResponse(events(), media_type="text/event-stream")

    app.frontend("/", directory=str(Path(__file__).parent / "static"))
    return app
