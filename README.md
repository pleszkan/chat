# Hexagonal OpenRouter Chat API

FastAPI chat service with application-owned users, sessions, roles, and conversation ownership.
Discord is the initial federated identity adapter; Discord tokens are used only to read the current
profile and are never persisted.

## Run locally

The project requires Python 3.14 and uses [uv](https://docs.astral.sh/uv/). Configure a Discord
application whose OAuth redirect URI exactly matches
`$APP_ORIGIN/v1/auth/oauth/discord/callback`, then set:

```bash
export APP_ORIGIN="https://chat.example.test"
export JWT_SECRET="a-random-secret-containing-at-least-32-bytes"
export DISCORD_CLIENT_ID="..."
export DISCORD_CLIENT_SECRET="..."
export OPENROUTER_API_KEY="..."
export OPENROUTER_MODEL="meta-llama/llama-3.3-70b-instruct:free" # optional
export DATABASE_URL="sqlite+aiosqlite:///./chat.db"              # optional
uv run fastapi dev main.py
```

OAuth and refresh cookies are always `Secure`, so browser login requires HTTPS, including during
local development. Put an HTTPS development proxy in front of FastAPI if needed. The Discord app
needs only the `identify` scope.

This release intentionally has no migration from the former anonymous schema. Delete the disposable
`chat.db` before first deployment and let the service recreate it.

Generation workers and the SSE event broker are process-local. Run the service as a single
application process (`--workers 1`) with one active instance; do not add multiple workers or
replicas until the broker is replaced with shared durable event delivery.

## Authentication and authorization

- Access JWTs are HS256 tokens with a 15-minute lifetime and remain valid until expiry.
- Refresh sessions have a fixed 30-day lifetime. Every refresh rotates the secret; replaying a
  correctly authenticated consumed token revokes the entire session family.
- All chat and generation endpoints require `Authorization: Bearer <access-token>`.
- Regular users can access only conversations they own. Admins can list, read, send to, stream, and
  delete every conversation. Unauthorized resource IDs return the same `404` as missing resources.
- Admin roles are managed directly in the database; there is no role-management API.

Promote the local user linked to a Discord ID with:

```sql
UPDATE users
SET role = 'admin'
WHERE id = (
  SELECT user_id
  FROM external_identities
  WHERE provider = 'discord' AND subject = '<Discord ID>'
);
```

The user must refresh their session (or sign in again) to receive an access token carrying the new
role. Existing access tokens are stateless and retain their original role until they expire.

## API

Public authentication routes:

- `GET /v1/auth/providers`
- `GET /v1/auth/oauth/{provider}/start`
- `GET /v1/auth/oauth/{provider}/callback`
- `POST /v1/auth/refresh`
- `POST /v1/auth/logout`
- `GET /v1/auth/me`

Refresh and logout requests require an `Origin` header exactly matching `APP_ORIGIN`. The refresh
credential is an HttpOnly cookie and is never exposed to browser JavaScript. The browser keeps its
access token only in memory.

Authenticated chat routes:

- `POST /v1/conversations` creates a conversation.
- `GET /v1/conversations` and `GET /v1/conversations/{id}` list/read permitted conversations.
- `DELETE /v1/conversations/{id}` deletes one.
- `POST /v1/conversations/{id}/messages` accepts `{ "text": "..." }`, returns `202`, and starts a
  detached generation. Send `Idempotency-Key` to protect a retry within the running process.
- `GET /v1/generations/{generation_id}/events?conversation_id={id}` streams authenticated SSE
  `snapshot`, `delta`, `completed`, or `failed` events.

The generation worker checkpoints every provider text chunk before broadcasting it. Disconnecting
an SSE client does not cancel the provider request; the completed or failed transcript remains
available through the conversation endpoint.

## Tests and code quality

```bash
uv run pytest -q
uv run python -m compileall -q app main.py
uv run ruff format --check
uv run ruff check
```
