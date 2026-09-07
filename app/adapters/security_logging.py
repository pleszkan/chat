import json
import logging


class SecurityJsonFormatter(logging.Formatter):
    fields = ("provider", "user_id", "session_id", "conversation_id")

    def format(self, record: logging.LogRecord) -> str:
        payload = {"event": record.getMessage(), "level": record.levelname}
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_security_logger() -> logging.Logger:
    logger = logging.getLogger("chat.security")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(SecurityJsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
