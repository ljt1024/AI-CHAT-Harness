"""Console logging for the service.

Everything logs through the standard library, so this module only decides where
records go and what they look like. Two shapes are supported: readable text for
a terminal and one JSON object per line for a log collector, selected with
``AGENTFORGE_LOG_FORMAT``. The level comes from ``AGENTFORGE_LOG_LEVEL``.

Every record carries the request id of the HTTP call that caused it. Runs,
tool calls and MCP discovery all happen inside a request, so a single id is
enough to reconstruct one interaction from an interleaved log.
"""
import json
import logging
import os
import sys
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Third-party loggers that are chatty at INFO and say nothing we cannot get
# from our own records.
QUIET = {
    "httpx": logging.WARNING, "httpcore": logging.WARNING, "mcp": logging.WARNING,
    "pymongo": logging.WARNING, "docker": logging.WARNING,
    # Our request middleware logs method, path, status and duration, which is
    # strictly more than the access log prints.
    "uvicorn.access": logging.WARNING,
}


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        return True


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s [%(request_id)s] %(message)s",
                         datefmt="%H:%M:%S")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                           "level": record.levelname, "logger": record.name,
                           "request_id": getattr(record, "request_id", "-"),
                           "message": record.getMessage()}, ensure_ascii=False)


def configure(level: str | None = None) -> logging.Logger:
    """Install one handler on the root logger, once per process."""
    logger = logging.getLogger("agentforge")
    if getattr(logger, "configured", False):
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())
    if os.getenv("AGENTFORGE_LOG_FORMAT", "text").strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel((level or os.getenv("AGENTFORGE_LOG_LEVEL", "INFO")).strip().upper())
    for name, quiet in QUIET.items():
        logging.getLogger(name).setLevel(quiet)
    logger.configured = True
    return logger
