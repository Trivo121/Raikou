"""Small dependency-free production observability primitives for the V1 API."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import re
import time
from threading import Lock
from typing import Iterator


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
request_context_var: ContextVar[dict[str, str]] = ContextVar("request_context", default={})
_request_id_pattern = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def safe_request_id(value: str | None) -> str | None:
    if value and _request_id_pattern.fullmatch(value):
        return value
    return None


class JsonFormatter(logging.Formatter):
    """Emit machine-readable logs without serialising request bodies or secrets."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update(request_context_var.get())
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, json_logs: bool) -> None:
    """Configure root logging once; Uvicorn keeps its own handlers intact."""
    root = logging.getLogger()
    if getattr(root, "_raikou_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root._raikou_configured = True  # type: ignore[attr-defined]


class Metrics:
    """In-process Prometheus text metrics for a single API process.

    Durable task state remains in PostgreSQL/Redis. These counters expose API,
    cache, model, and upload failure signals for scraping without accepting
    untrusted metric labels.
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._durations: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
        self._lock = Lock()

    @staticmethod
    def _labels(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))

    def increment(self, name: str, labels: dict[str, str] | None = None, value: float = 1) -> None:
        with self._lock:
            self._counters[(name, self._labels(labels))] += value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            samples = self._durations[(name, self._labels(labels))]
            samples.append(float(value))
            if len(samples) > 2_000:
                del samples[:1_000]

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_render_labels(labels)} {value}")
            for (name, labels), samples in sorted(self._durations.items()):
                if samples:
                    lines.append(f"{name}_count{_render_labels(labels)} {len(samples)}")
                    lines.append(f"{name}_sum{_render_labels(labels)} {sum(samples):.6f}")
        return "\n".join(lines) + "\n"


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{value.replace(chr(34), chr(92) + chr(34))}"' for key, value in labels)
    return "{" + rendered + "}"


metrics = Metrics()


@contextmanager
def request_scope(request_id: str, **context: str) -> Iterator[None]:
    """Context manager for structured logs issued during one request."""
    request_token = request_id_var.set(request_id)
    context_token = request_context_var.set({key: value for key, value in context.items() if value})
    try:
        yield
    finally:
        request_context_var.reset(context_token)
        request_id_var.reset(request_token)
