import json
import logging

from app.adapters.security_logging import SecurityJsonFormatter


def test_security_formatter_emits_only_allowlisted_structured_fields():
    record = logging.LogRecord(
        "chat.security", logging.INFO, __file__, 1, "auth.login_succeeded", (), None
    )
    record.provider = "discord"
    record.user_id = "user-1"
    record.code = "must-not-leak"

    payload = json.loads(SecurityJsonFormatter().format(record))

    assert payload == {
        "event": "auth.login_succeeded",
        "level": "INFO",
        "provider": "discord",
        "user_id": "user-1",
    }
