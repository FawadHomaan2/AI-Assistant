"""Structured logging.

Console output is human-readable during development; the file sink is JSON
lines so the Activity view and later tooling can parse it. Every record passes
through the redaction processor first.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any

import structlog

from jarvis.config import paths
from jarvis.util.redaction import redaction_processor

_configured = False


def configure(level: str = "INFO", *, to_file: bool = True, json_console: bool = False) -> None:
    """Install structlog + stdlib logging. Idempotent."""
    global _configured
    if _configured:
        return

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Redaction runs last among the shared processors, so anything an
        # earlier processor added is covered too.
        redaction_processor,
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    # stderr, because stdout carries the handshake line the Rust shell parses.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer()
                if json_console
                else structlog.dev.ConsoleRenderer(colors=False),
            ],
        )
    )
    root.addHandler(console)

    if to_file:
        try:
            paths.ensure_dirs()
            file_handler = logging.handlers.RotatingFileHandler(
                paths.log_dir() / "jarvis.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                structlog.stdlib.ProcessorFormatter(
                    processors=[
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.processors.JSONRenderer(),
                    ],
                )
            )
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("file logging unavailable: %s", exc)

    # Uvicorn's own loggers would otherwise duplicate every line.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def reset() -> None:
    """Test helper: allow reconfiguration."""
    global _configured
    _configured = False
