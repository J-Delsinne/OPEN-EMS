from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger


def _add_component(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Map logger name to 'component' field if not explicitly set.

    Handles two cases:
    - structlog loggers: logger is a BoundLogger with a .name attribute
    - stdlib-bridged records (foreign_pre_chain): logger is a plain str
    """
    if "component" not in event_dict:
        if isinstance(logger, str):
            event_dict["component"] = logger
        else:
            name = getattr(logger, "name", None)
            event_dict["component"] = name if name else str(logger)
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog as sole logging backend with JSON stdout output.

    Must be called once at application startup before any logger is created.
    Bridges stdlib logging (uvicorn, alembic, SQLAlchemy) to the same JSON sink.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_component,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    # Replace ALL root logger handlers to prevent plain-text output alongside JSON.
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
