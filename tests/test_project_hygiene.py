from pathlib import Path


def test_sqlite_chat_db_sidecars_are_ignored():
    ignored = set(Path(".gitignore").read_text().splitlines())

    assert {"chat.db-journal", "chat.db-wal", "chat.db-shm"} <= ignored
