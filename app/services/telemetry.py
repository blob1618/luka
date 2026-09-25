"""Pipeline telemetry and latency instrumentation."""

import contextvars
import logging
import threading
import time
from typing import Any

logger = logging.getLogger("luka.metrics")
sql_logger = logging.getLogger("luka.telemetry.sql")

_current_telemetry: contextvars.ContextVar["MessageTelemetry | None"] = (
    contextvars.ContextVar("current_telemetry", default=None)
)

KNOWN_PHASES = ("reaction", "redis", "llm", "db", "reply")


class MessageTelemetry:
    """Tracks latency metrics across execution phases for a message."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self.start_time = time.perf_counter()
        self.phases: dict[str, float] = {}
        self._lock = threading.RLock()
        self.sql_count: int = 0
        self.sql_exec_ms: float = 0.0

    def increment_sql_count(self) -> None:
        with self._lock:
            self.sql_count += 1

    def accumulate_sql_time(self, duration_ms: float) -> None:
        with self._lock:
            self.sql_exec_ms += duration_ms

    def record_phase(self, phase: str, duration_ms: float) -> None:
        with self._lock:
            self.phases[phase] = self.phases.get(phase, 0.0) + duration_ms

    def finish(self, status: str = "completed") -> dict[str, Any]:
        total_ms = (time.perf_counter() - self.start_time) * 1000

        with self._lock:
            sql_count = self.sql_count
            sql_exec_ms = self.sql_exec_ms
            phases_snapshot = dict(self.phases)

        result: dict[str, Any] = {
            "message_id": self.message_id,
            "status": status,
            "total_ms": round(total_ms, 2),
        }

        parts = [
            "[METRICS]",
            f"message_id={self.message_id}",
            f"status={status}",
            f"total_ms={total_ms:.2f}",
        ]

        for phase in KNOWN_PHASES:
            if phase in phases_snapshot:
                duration = round(phases_snapshot[phase], 2)
                result[f"{phase}_ms"] = duration
                parts.append(f"{phase}_ms={duration:.2f}")

        for phase, duration_val in phases_snapshot.items():
            if phase not in KNOWN_PHASES:
                duration = round(duration_val, 2)
                result[f"{phase}_ms"] = duration
                parts.append(f"{phase}_ms={duration:.2f}")

        logger.info(" ".join(parts))

        sql_logger.info(
            f"[SQL_METRICS_ANONYMOUS] sql_count={sql_count} sql_exec_ms={sql_exec_ms:.2f}"
        )

        return result


def start_message_telemetry(message_id: str) -> MessageTelemetry:
    telemetry = MessageTelemetry(message_id)
    _current_telemetry.set(telemetry)
    return telemetry


def get_current_telemetry() -> MessageTelemetry | None:
    return _current_telemetry.get()


def finish_message_telemetry(status: str = "completed") -> dict[str, Any] | None:
    telemetry = _current_telemetry.get()
    if telemetry is None:
        return None
    metrics = telemetry.finish(status=status)
    _current_telemetry.set(None)
    return metrics


class track_phase:
    """Context manager supporting both sync and async blocks for phase timing."""

    def __init__(self, phase: str):
        self.phase = phase
        self.start: float = 0.0

    def __enter__(self) -> "track_phase":
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        duration_ms = (time.perf_counter() - self.start) * 1000
        telemetry = get_current_telemetry()
        if telemetry is not None:
            telemetry.record_phase(self.phase, duration_ms)

    async def __aenter__(self) -> "track_phase":
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)
