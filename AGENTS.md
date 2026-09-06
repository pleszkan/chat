# Repository Guidelines

## Project Structure & Module Organization

- `app/domain/` contains framework-free conversation, message, and generation rules.
- `app/application/` holds use cases and port protocols that depend on the domain, not on FastAPI or SQLAlchemy.
- `app/adapters/` contains infrastructure implementations: SQLAlchemy persistence and the OpenRouter streaming client.
- `app/api.py` composes dependencies and defines REST/SSE routes; `main.py` exposes the ASGI app.
- `dist/index.html` is the dependency-free browser UI served at `/`.
- `tests/` mirrors behavior by layer: domain, application, adapters, API, and frontend-serving tests.

## Build, Test, and Development Commands

Use `uv` with Python 3.12 or newer:

```bash
uv run fastapi dev main.py       # Start the API and static frontend locally
uv run pytest -q                 # Run the complete test suite
uv run pytest tests/test_api.py -q  # Run one focused test module
uv run python -m compileall -q app main.py  # Check Python syntax/import compilation
```

Set `OPENROUTER_API_KEY` before running against OpenRouter. `OPENROUTER_MODEL` and `DATABASE_URL` are optional; SQLite defaults to `./chat.db`.

## Coding Style & Naming Conventions

Use four-space Python indentation, type annotations for public functions, `snake_case` for functions/modules, and `PascalCase` for classes. Keep domain and application code independent of HTTP, ORM, and provider imports. Prefer small, focused modules and dependency injection through ports. The frontend is plain HTML/CSS/JavaScript; keep it dependency-free and use stable element IDs such as `message-form` for testable UI hooks.

## Testing Guidelines

Use `pytest` and `pytest-asyncio`. Name files `tests/test_<area>.py` and tests `test_<observable_behavior>()`. Add a failing test before changing behavior, then run the focused test and full suite. Use real SQLite in repository tests and mock only the external OpenRouter HTTP boundary. Cover failures that preserve partial assistant output and generation state.

## Commit & Pull Request Guidelines

No Git history is available in this workspace. Use concise imperative Conventional Commit-style messages, for example `feat: add conversation sidebar` or `fix: preserve partial provider output`. PRs should describe user-visible behavior, list tests run, link relevant issues, and include a screenshot for changes to `dist/index.html`.

## Security & Configuration

Never commit `.env`, API keys, or SQLite data files. Keep `OPENROUTER_API_KEY` in the environment and never return it through API responses or frontend code.
