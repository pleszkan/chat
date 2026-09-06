# Hexagonal OpenRouter Chat API

FastAPI backend for an anonymous chat demo. Conversation rules live in the domain layer; FastAPI, SQLAlchemy/SQLite, and OpenRouter are adapters.

## Run locally

This project requires Python 3.14 and uses [uv](https://docs.astral.sh/uv/).

```bash
export OPENROUTER_API_KEY="..."
export OPENROUTER_MODEL="meta-llama/llama-3.3-70b-instruct:free"
uv run fastapi dev main.py
```

`DATABASE_URL` defaults to `sqlite+aiosqlite:///./chat.db`.
Open `http://127.0.0.1:8000/` to use the included minimal chat UI.

## API

- `POST /v1/conversations` creates a conversation.
- `GET /v1/conversations` and `GET /v1/conversations/{id}` list/read conversations.
- `DELETE /v1/conversations/{id}` deletes one.
- `POST /v1/conversations/{id}/messages` accepts `{ "text": "..." }`, returns `202`, and starts a detached generation. Send `Idempotency-Key` to protect a client retry within the running process.
- `GET /v1/generations/{generation_id}/events?conversation_id={id}` streams SSE `snapshot`, `delta`, `completed`, or `failed` events.

The generation worker checkpoints every provider text chunk before broadcasting it. Disconnecting an SSE client does not cancel the provider request; the completed or failed transcript remains available through the conversation endpoint.

## Tests

```bash
uv run pytest -q
```

## Code quality

```bash
uv run ruff format --check
uv run ruff check
```

To apply Ruff's safe fixes locally:

```bash
uv run ruff format
uv run ruff check --fix
```
# chat
