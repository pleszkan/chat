# Pluggable Authentication Handoff

## Resume here

The implementation is in the isolated worktree:

```bash
cd /home/a/dev/chat/.worktrees/pluggable-auth
git branch --show-current
git status --short
```

Expected branch: `feat/pluggable-auth`.

Base commit: `b8f511ae87b0e0a9ac81eaaedc45e3940f34f134` on `master`.
All authentication changes are currently uncommitted. The main checkout was not
modified by this work.

## Current verification state

The following checks passed immediately before this handoff was written:

```bash
uv run ruff format
uv run ruff check
uv run pytest -q
uv run python -m compileall -q app main.py
```

Results:

- Formatting: 30 files unchanged
- Lint: all checks passed
- Tests: 53 passed, 1 warning
- Compile check: passed

The warning is an upstream Starlette deprecation warning emitted through
`fastapi.testclient`; it is not an application test failure.

## What is implemented

- Provider-neutral auth domain models for users, external identities, roles,
  principals, OAuth transactions, and refresh sessions.
- Required conversation ownership with owner-only access for regular users and
  full chat access for administrators.
- Public chat use cases separated from trusted background generation updates.
- `AuthService` covering OAuth start/callback, automatic provisioning, access
  token issuance, refresh rotation, replay-family revocation, logout, and current
  user lookup.
- SQLAlchemy persistence for users, multiple external identities, one-use OAuth
  transactions, refresh families/generations, and indexed conversation owners.
- Atomic refresh consumption, hashed secrets, exact expiry handling, cleanup,
  SQLite foreign-key enforcement, and concurrency coverage.
- Discord OAuth adapter using authorization-code exchange and the `identify`
  scope; Discord access tokens are discarded after `/users/@me` is fetched.
- HS256 JWT codec with fixed algorithm, issuer, audience, required claims,
  temporal validation, and a minimum 32-byte secret.
- Generic auth API routes, secure state/refresh cookies, exact Origin checks,
  bearer authentication, owner/admin authorization, authenticated SSE, and
  structured allowlisted security logs.
- Dependency-free frontend login/bootstrap/logout flow, in-memory access tokens,
  single-flight refresh/retry behavior, and fetch-based authenticated SSE.
- Configuration, deployment reset, Discord setup, role-promotion SQL, and API
  behavior documented in `README.md`.

## Review notes already addressed

A code-review pass identified and the implementation fixed refresh concurrency,
identity-provisioning races, JWT clock handling, security-log allowlisting,
authorization-matrix gaps, SQLite foreign keys, application/HTTP coupling,
parallel OAuth state cookies, invalid-refresh cookie clearing, no-store auth
responses, refresh cleanup, and logout error handling.

The SSE snapshot/register ordering was deliberately retained. The database read
is awaited first; queue lookup and registration then execute without an await on
the same event loop, so no coroutine can publish between those two operations.
Registering before the snapshot read could instead duplicate deltas already
present in that snapshot.

## Remaining work

1. Review the full diff and ensure no unrelated user changes are present:

   ```bash
   git diff --stat
   git diff
   ```

2. Re-run the verification commands above after making any further edits.

3. Perform the real-provider smoke test when Discord credentials and an HTTPS
   development origin are available. Required configuration:

   ```text
   APP_ORIGIN=https://your-development-origin.example
   JWT_SECRET=<random secret of at least 32 bytes>
   DISCORD_CLIENT_ID=<Discord application client ID>
   DISCORD_CLIENT_SECRET=<Discord application client secret>
   OPENROUTER_API_KEY=<needed for real chat generation>
   ```

   Register this callback in the Discord application:

   ```text
   https://your-development-origin.example/v1/auth/oauth/discord/callback
   ```

   Existing anonymous SQLite data is intentionally not migrated. Remove or move
   the disposable development database before starting this schema version.
   Do not commit credentials or database files.

4. Manually smoke-test: Discord login, reload restoration through refresh,
   access-token refresh, concurrent protected requests, logout, cross-user 404
   isolation, administrator access, authenticated streaming, and partial-output
   preservation on provider failure. Automated tests already cover these flows
   with test providers; this step validates browser/cookie/proxy/provider wiring.

5. If the review is satisfactory, commit from this worktree, for example:

   ```bash
   git add README.md app pyproject.toml tests uv.lock CONTINUATION.md
   git commit -m "feat: add pluggable authentication and authorization"
   ```

6. Then choose the desired integration path: merge into `master`, push the
   feature branch and open a PR, or keep the worktree for further development.
   Do not delete the worktree before committing or otherwise preserving its
   uncommitted changes.

## Useful focused test commands

```bash
uv run pytest tests/test_auth_application.py -q
uv run pytest tests/test_auth_adapters.py -q
uv run pytest tests/test_sqlalchemy_repository.py -q
uv run pytest tests/test_api.py -q
uv run pytest tests/test_frontend.py -q
```
