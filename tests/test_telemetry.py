import asyncio
import logging
import time
from unittest.mock import patch

from app.services.telemetry import (
    MessageTelemetry,
    finish_message_telemetry,
    get_current_telemetry,
    start_message_telemetry,
    track_phase,
)


def test_telemetry_records_phases_and_accumulates(caplog):
    caplog.set_level(logging.INFO, logger="luka.metrics")
    telemetry = MessageTelemetry(message_id="wamid.123")

    telemetry.record_phase("redis", 15.5)
    telemetry.record_phase("redis", 10.5)
    telemetry.record_phase("llm", 200.0)

    with patch("time.perf_counter", side_effect=[100.0, 101.5]):
        # Mocking start and finish time for deterministic total_ms
        t = MessageTelemetry(message_id="wamid.123")
        t.record_phase("redis", 26.0)
        t.record_phase("llm", 200.0)
        metrics = t.finish(status="completed")

    assert metrics["message_id"] == "wamid.123"
    assert metrics["status"] == "completed"
    assert metrics["total_ms"] == 1500.0
    assert metrics["redis_ms"] == 26.0
    assert metrics["llm_ms"] == 200.0
    # Phases not executed should be omitted
    assert "reaction_ms" not in metrics
    assert "db_ms" not in metrics
    assert "reply_ms" not in metrics

    # Verify log output
    assert "luka.metrics" in caplog.records[0].name
    assert "[METRICS]" in caplog.text
    assert "message_id=wamid.123" in caplog.text
    assert "status=completed" in caplog.text
    assert "total_ms=1500.00" in caplog.text
    assert "redis_ms=26.00" in caplog.text
    assert "llm_ms=200.00" in caplog.text


def test_track_phase_sync_and_async_context_manager():
    start_message_telemetry("wamid.ctx")

    with track_phase("db"):
        time.sleep(0.01)

    async def async_phase():
        async with track_phase("llm"):
            await asyncio.sleep(0.01)

    asyncio.run(async_phase())

    metrics = finish_message_telemetry(status="completed")
    assert metrics is not None
    assert metrics["message_id"] == "wamid.ctx"
    assert metrics["status"] == "completed"
    assert metrics["db_ms"] >= 8.0  # at least ~10ms
    assert metrics["llm_ms"] >= 8.0
    assert get_current_telemetry() is None


def test_track_phase_outside_telemetry_does_not_fail():
    assert get_current_telemetry() is None
    # Calling track_phase when no telemetry is active should not raise
    with track_phase("reaction"):
        pass

    async def run_async():
        async with track_phase("reaction"):
            pass

    asyncio.run(run_async())


def test_telemetry_privacy_guarantee(caplog):
    caplog.set_level(logging.INFO, logger="luka.metrics")
    start_message_telemetry("wamid.private")
    with track_phase("reaction"):
        pass
    finish_message_telemetry(status="completed")

    log_content = caplog.text
    # Ensure no sensitive keywords appear
    for forbidden in ("phone", "549", "5411", "text", "body", "prompt", "token", "monto", "amount"):
        assert f"{forbidden}=" not in log_content


def test_telemetry_concurrency_isolation_between_contextvars():
    async def task_one():
        start_message_telemetry("wamid.concurrent.1")
        async with track_phase("reaction"):
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.01)  # allow task_two to interleave
        async with track_phase("redis"):
            await asyncio.sleep(0.02)
        return finish_message_telemetry("completed")

    async def task_two():
        start_message_telemetry("wamid.concurrent.2")
        async with track_phase("llm"):
            await asyncio.sleep(0.025)
        await asyncio.sleep(0.01)  # allow task_one to interleave
        async with track_phase("db"):
            await asyncio.sleep(0.015)
        return finish_message_telemetry("completed")

    async def run_both():
        return await asyncio.gather(task_one(), task_two())

    metrics1, metrics2 = asyncio.run(run_both())

    assert metrics1["message_id"] == "wamid.concurrent.1"
    assert "reaction_ms" in metrics1
    assert "redis_ms" in metrics1
    assert "llm_ms" not in metrics1
    assert "db_ms" not in metrics1

    assert metrics2["message_id"] == "wamid.concurrent.2"
    assert "llm_ms" in metrics2
    assert "db_ms" in metrics2
    assert "reaction_ms" not in metrics2
    assert "redis_ms" not in metrics2

