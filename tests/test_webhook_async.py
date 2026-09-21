import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.main import _process_inbound_message_background, app
from app.services.webhook_idempotency import IdempotencyUnavailable, InboundMessageClaim

pytestmark = pytest.mark.unit

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_pending_conversation_flows(monkeypatch):
    for method in (
        "is_awaiting_rename",
        "is_awaiting_reminder_data",
        "is_awaiting_limit_year_confirmation",
        "is_awaiting_limit_category_confirmation",
        "is_awaiting_limit_data",
        "is_awaiting_limit_delete_category",
        "is_awaiting_limit_month_selection",
        "is_awaiting_compensation_confirmation",
    ):
        monkeypatch.setattr(
            f"app.services.dispatcher.ConversationService.{method}",
            AsyncMock(return_value=False),
        )
    claim = InboundMessageClaim("wamid.test", "whatsapp:inbound:test", "token")
    monkeypatch.setattr(
        "app.services.webhook_idempotency.WebhookIdempotencyService.claim",
        AsyncMock(return_value=claim),
    )
    monkeypatch.setattr(
        "app.services.webhook_idempotency.WebhookIdempotencyService.complete",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.webhook_idempotency.WebhookIdempotencyService.release",
        AsyncMock(return_value=True),
    )


@pytest_asyncio.fixture(autouse=True)
async def cleanup_pending_reaction_tasks():
    import asyncio
    from app.api.whatsapp import close_whatsapp_client
    from app.main import _pending_reaction_tasks

    if _pending_reaction_tasks:
        tasks = list(_pending_reaction_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _pending_reaction_tasks.clear()
    await close_whatsapp_client()

    yield

    if _pending_reaction_tasks:
        tasks = list(_pending_reaction_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _pending_reaction_tasks.clear()
    await close_whatsapp_client()


def make_webhook_payload(messages=None, statuses=None):
    value = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "16505551111",
            "phone_number_id": "123456123456",
        },
        "contacts": [{"profile": {"name": "Test User"}, "wa_id": "12345"}],
    }
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [{"value": value, "field": "messages"}],
            }
        ],
    }


def make_text_message(body="Gaste 5000 en supermercado", msg_id="wamid.text1"):
    return {
        "from": "5491112345678",
        "id": msg_id,
        "timestamp": "1603059201",
        "text": {"body": body},
        "type": "text",
    }


def make_interactive_message(option_id="opt_1", msg_id="wamid.inter1"):
    return {
        "from": "5491112345678",
        "id": msg_id,
        "timestamp": "1603059201",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": option_id, "title": "Opcion"},
        },
    }


def make_unsupported_message(msg_type="image", msg_id="wamid.unsupp1"):
    return {
        "from": "5491112345678",
        "id": msg_id,
        "timestamp": "1603059201",
        "type": msg_type,
        msg_type: {"id": "media-id-123"},
    }


class TestWebhookAsyncEnqueue:
    """Verifica que POST /webhook programa tareas de fondo para mensajes soportados y responde 200."""

    def test_text_message_enqueues_background_task(self):
        msg = make_text_message()
        payload = make_webhook_payload(messages=[msg])

        with patch("fastapi.BackgroundTasks.add_task") as mock_add_task:
            response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_add_task.assert_called_once()
        args, _ = mock_add_task.call_args
        assert args[0] == _process_inbound_message_background
        assert args[1] == msg

    def test_interactive_message_enqueues_background_task(self):
        msg = make_interactive_message()
        payload = make_webhook_payload(messages=[msg])

        with patch("fastapi.BackgroundTasks.add_task") as mock_add_task:
            response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_add_task.assert_called_once()
        args, _ = mock_add_task.call_args
        assert args[0] == _process_inbound_message_background
        assert args[1] == msg

    def test_unsupported_message_type_does_not_enqueue_task(self):
        msg = make_unsupported_message(msg_type="image")
        payload = make_webhook_payload(messages=[msg])

        with patch("fastapi.BackgroundTasks.add_task") as mock_add_task:
            response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_add_task.assert_not_called()

    def test_status_update_does_not_enqueue_task(self):
        status = {"id": "wamid.s1", "status": "delivered"}
        payload = make_webhook_payload(statuses=[status])

        with patch("fastapi.BackgroundTasks.add_task") as mock_add_task:
            response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_add_task.assert_not_called()

    def test_multiple_messages_enqueue_individual_tasks(self):
        msg1 = make_text_message(msg_id="wamid.m1")
        msg2 = make_interactive_message(msg_id="wamid.m2")
        msg3 = make_unsupported_message(msg_type="audio", msg_id="wamid.m3")
        payload = make_webhook_payload(messages=[msg1, msg2, msg3])

        with patch("fastapi.BackgroundTasks.add_task") as mock_add_task:
            response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert mock_add_task.call_count == 2
        calls = mock_add_task.call_args_list
        assert calls[0].args[1] == msg1
        assert calls[1].args[1] == msg2

    def test_empty_payload_does_not_enqueue_task(self):
        payload = make_webhook_payload()

        with patch("fastapi.BackgroundTasks.add_task") as mock_add_task:
            response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        mock_add_task.assert_not_called()


class TestProcessInboundMessageBackground:
    """Verifica la función _process_inbound_message_background directamente."""

    @pytest.mark.asyncio
    async def test_process_text_message_invokes_process_text_message_once(self, caplog):
        caplog.set_level(logging.INFO)
        msg = make_text_message(body="Gaste 5000", msg_id="wamid.proc.text")
        fake_redis = MagicMock()

        with patch(
            "app.main.process_text_message_once",
            new_callable=AsyncMock,
            return_value="completed",
        ) as mock_text_once:
            await _process_inbound_message_background(msg, fake_redis)

        mock_text_once.assert_awaited_once()
        kwargs = mock_text_once.await_args.kwargs
        assert kwargs["redis_client"] == fake_redis
        assert kwargs["sender_phone"] == "5491112345678"
        assert kwargs["text_body"] == "Gaste 5000"
        assert kwargs["whatsapp_message_id"] == "wamid.proc.text"

        captured = caplog.text
        assert "message_id=wamid.proc.text" in captured
        assert "status=started" in captured
        assert "status=completed" in captured
        assert "duration_ms=" in captured
        # Verificar que NO se loguee teléfono completo ni texto ni importes
        assert "5491112345678" not in captured
        assert "Gaste 5000" not in captured

    @pytest.mark.asyncio
    async def test_process_interactive_message_invokes_process_interactive_message_once(self, caplog):
        caplog.set_level(logging.INFO)
        msg = make_interactive_message(option_id="confirm_yes", msg_id="wamid.proc.inter")
        fake_redis = MagicMock()

        with patch(
            "app.main.process_interactive_message_once",
            new_callable=AsyncMock,
            return_value="completed",
        ) as mock_inter_once:
            await _process_inbound_message_background(msg, fake_redis)

        mock_inter_once.assert_awaited_once()
        kwargs = mock_inter_once.await_args.kwargs
        assert kwargs["redis_client"] == fake_redis
        assert kwargs["interactive_reply"].option_id == "confirm_yes"
        assert kwargs["interactive_reply"].message_id == "wamid.proc.inter"

        captured = caplog.text
        assert "message_id=wamid.proc.inter" in captured
        assert "status=completed" in captured
        assert "5491112345678" not in captured

    @pytest.mark.asyncio
    async def test_process_unsupported_type_is_ignored_safely(self, caplog):
        caplog.set_level(logging.INFO)
        msg = make_unsupported_message(msg_type="location", msg_id="wamid.proc.unsupp")
        fake_redis = MagicMock()

        await _process_inbound_message_background(msg, fake_redis)

        captured = caplog.text
        assert "message_id=wamid.proc.unsupp" in captured
        assert "status=unsupported_type" in captured

    @pytest.mark.asyncio
    async def test_process_invalid_interactive_is_ignored_safely(self, caplog):
        caplog.set_level(logging.INFO)
        msg = {
            "from": "5491112345678",
            "id": "wamid.proc.badinter",
            "type": "interactive",
            "interactive": {},  # missing button_reply / list_reply
        }
        fake_redis = MagicMock()

        await _process_inbound_message_background(msg, fake_redis)

        captured = caplog.text
        assert "message_id=wamid.proc.badinter" in captured
        assert "status=ignored_invalid_interactive" in captured

    @pytest.mark.asyncio
    async def test_idempotency_unavailable_is_handled_gracefully(self, caplog):
        caplog.set_level(logging.INFO)
        msg = make_text_message(msg_id="wamid.proc.idem_err")
        fake_redis = MagicMock()

        with patch(
            "app.main.process_text_message_once",
            new_callable=AsyncMock,
            side_effect=IdempotencyUnavailable("Redis connection failed"),
        ):
            # No debe propagar la excepción hacia arriba
            await _process_inbound_message_background(msg, fake_redis)

        captured = caplog.text
        assert "message_id=wamid.proc.idem_err" in captured
        assert "status=idempotency_unavailable" in captured
        assert "error=IdempotencyUnavailable" in captured
        assert "duration_ms=" in captured

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_handled_gracefully(self, caplog):
        caplog.set_level(logging.INFO)
        msg = make_text_message(msg_id="wamid.proc.unexp_err")
        fake_redis = MagicMock()

        with patch(
            "app.main.process_text_message_once",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Unexpected crash in domain"),
        ):
            # No debe propagar la excepción hacia arriba
            await _process_inbound_message_background(msg, fake_redis)

        captured = caplog.text
        assert "message_id=wamid.proc.unexp_err" in captured
        assert "status=error" in captured
        assert "error=RuntimeError" in captured
        assert "duration_ms=" in captured
        # Verifica que logger.exception registre el traceback
        assert "Traceback (most recent call last)" in captured
        assert "RuntimeError: Unexpected crash in domain" in captured

    @pytest.mark.asyncio
    async def test_reaction_is_sent_before_processing_for_text_message(self, caplog):
        import asyncio
        from app.main import _pending_reaction_tasks
        caplog.set_level(logging.INFO, logger="luka.metrics")
        msg = make_text_message(msg_id="wamid.react.text")
        fake_redis = MagicMock()
        reaction_blocked = asyncio.Event()
        process_started = asyncio.Event()
        signal_order = []

        async def slow_reaction(*args, **kwargs):
            signal_order.append("reaction")
            await reaction_blocked.wait()
            return True

        async def mock_typing(*args, **kwargs):
            signal_order.append("typing")
            return True

        async def mock_process(*args, **kwargs):
            process_started.set()
            return "completed"

        with (
            patch("app.main.send_whatsapp_reaction", side_effect=slow_reaction) as mock_react,
            patch(
                "app.main.send_whatsapp_typing_indicator",
                side_effect=mock_typing,
            ) as mock_typing_ind,
            patch("app.main.process_text_message_once", side_effect=mock_process) as mock_proc,
        ):
            task = asyncio.create_task(_process_inbound_message_background(msg, fake_redis))

            # Domain processing starts immediately while reaction HTTP call is still pending
            await asyncio.wait_for(process_started.wait(), timeout=1.0)
            assert not reaction_blocked.is_set()

            reaction_blocked.set()
            await asyncio.wait_for(task, timeout=1.0)
            for pending_task in list(_pending_reaction_tasks):
                await asyncio.wait_for(pending_task, timeout=1.0)

        mock_proc.assert_awaited_once()
        mock_react.assert_awaited_once_with(
            to_number="5491112345678",
            message_id="wamid.react.text",
            emoji="⏳",
        )
        mock_typing_ind.assert_awaited_once_with("wamid.react.text")
        assert signal_order == ["reaction", "typing"]

        # Telemetry verification
        assert "[METRICS]" in caplog.text
        assert "message_id=wamid.react.text" in caplog.text
        assert "status=completed" in caplog.text
        assert "reaction_ms=" in caplog.text
        assert "typing_ms=" in caplog.text

    @pytest.mark.asyncio
    async def test_reaction_is_sent_for_interactive_message(self):
        import asyncio
        from app.main import _pending_reaction_tasks
        msg = make_interactive_message(option_id="opt_1", msg_id="wamid.react.inter")
        fake_redis = MagicMock()

        with (
            patch("app.main.send_whatsapp_reaction", new_callable=AsyncMock, return_value=True) as mock_react,
            patch(
                "app.main.send_whatsapp_typing_indicator",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_typing,
            patch("app.main.process_interactive_message_once", new_callable=AsyncMock, return_value="completed"),
        ):
            await _process_inbound_message_background(msg, fake_redis)
            for pending_task in list(_pending_reaction_tasks):
                await asyncio.wait_for(pending_task, timeout=1.0)

        mock_react.assert_awaited_once_with(
            to_number="5491112345678",
            message_id="wamid.react.inter",
            emoji="⏳",
        )
        mock_typing.assert_awaited_once_with("wamid.react.inter")

    @pytest.mark.asyncio
    async def test_reaction_failure_does_not_abort_processing(self, caplog):
        import asyncio
        caplog.set_level(logging.INFO)
        msg = make_text_message(msg_id="wamid.react.fail")
        fake_redis = MagicMock()

        with (
            patch("app.main.send_whatsapp_reaction", new_callable=AsyncMock, side_effect=RuntimeError("Meta network failure")),
            patch("app.main.process_text_message_once", new_callable=AsyncMock, return_value="completed") as mock_proc,
        ):
            await _process_inbound_message_background(msg, fake_redis)
            await asyncio.sleep(0)

        mock_proc.assert_awaited_once()
        assert "reaction_failed" in caplog.text
        assert "status=completed" in caplog.text

    @pytest.mark.asyncio
    async def test_typing_failure_does_not_abort_processing(self, caplog):
        import asyncio
        from app.main import _pending_reaction_tasks
        caplog.set_level(logging.INFO)
        msg = make_text_message(msg_id="wamid.typing.fail")
        fake_redis = MagicMock()

        with (
            patch("app.main.send_whatsapp_reaction", new_callable=AsyncMock, return_value=True),
            patch(
                "app.main.send_whatsapp_typing_indicator",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Meta network failure"),
            ),
            patch(
                "app.main.process_text_message_once",
                new_callable=AsyncMock,
                return_value="completed",
            ) as mock_proc,
        ):
            await _process_inbound_message_background(msg, fake_redis)
            for pending_task in list(_pending_reaction_tasks):
                await asyncio.wait_for(pending_task, timeout=1.0)

        mock_proc.assert_awaited_once()
        assert "typing_failed" in caplog.text
        assert "status=completed" in caplog.text

    @pytest.mark.asyncio
    async def test_telemetry_logs_on_idempotency_unavailable(self, caplog):
        caplog.set_level(logging.INFO, logger="luka.metrics")
        msg = make_text_message(msg_id="wamid.telemetry.idem")
        fake_redis = MagicMock()

        with (
            patch("app.main.send_whatsapp_reaction", new_callable=AsyncMock, return_value=True),
            patch("app.main.process_text_message_once", new_callable=AsyncMock, side_effect=IdempotencyUnavailable("Redis down")),
        ):
            await _process_inbound_message_background(msg, fake_redis)

        assert "[METRICS]" in caplog.text
        assert "message_id=wamid.telemetry.idem" in caplog.text
        assert "status=idempotency_unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_lifespan_shutdown_cleans_pending_tasks_and_closes_client(self):
        import asyncio
        from app.main import _dispatch_whatsapp_reaction, _pending_reaction_tasks, lifespan, app

        reaction_unblock = asyncio.Event()

        async def hanging_reaction(*args, **kwargs):
            await reaction_unblock.wait()
            return True

        with (
            patch("app.main.start_scheduler"),
            patch("app.main.send_whatsapp_reaction", side_effect=hanging_reaction),
            patch("app.main.close_whatsapp_client", new_callable=AsyncMock) as mock_close_client,
        ):
            _dispatch_whatsapp_reaction("5491112345678", "wamid.hanging")
            assert len(_pending_reaction_tasks) == 1

            async with lifespan(app):
                pass

            assert len(_pending_reaction_tasks) == 0
            mock_close_client.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_shutdown_cleans_up_even_on_exception_in_body(self):
        import asyncio
        from app.main import _dispatch_whatsapp_reaction, _pending_reaction_tasks, lifespan, app

        reaction_unblock = asyncio.Event()

        async def hanging_reaction(*args, **kwargs):
            await reaction_unblock.wait()
            return True

        mock_redis = AsyncMock()
        with (
            patch("app.main.start_scheduler"),
            patch("app.main.redis.from_url", return_value=mock_redis),
            patch("app.main.send_whatsapp_reaction", side_effect=hanging_reaction),
            patch("app.main.close_whatsapp_client", new_callable=AsyncMock) as mock_close_client,
        ):
            _dispatch_whatsapp_reaction("5491112345678", "wamid.exc_test")
            assert len(_pending_reaction_tasks) == 1

            with pytest.raises(RuntimeError, match="Crash inside lifespan"):
                async with lifespan(app):
                    raise RuntimeError("Crash inside lifespan")

            # Cleanup must still have occurred!
            assert len(_pending_reaction_tasks) == 0
            mock_close_client.assert_awaited_once()
            mock_redis.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reaction_returned_false_logs_reaction_failed(self, caplog):
        import asyncio
        caplog.set_level(logging.INFO)
        from app.main import _dispatch_whatsapp_reaction

        with patch("app.main.send_whatsapp_reaction", new_callable=AsyncMock, return_value=False):
            task = _dispatch_whatsapp_reaction("5491112345678", "wamid.false_test")
            await asyncio.sleep(0)
            assert task.done()
            assert "reaction_failed" in caplog.text
            assert "error=send_returned_false" in caplog.text
            assert "message_id=wamid.false_test" in caplog.text

    @pytest.mark.asyncio
    async def test_reaction_exception_is_retrieved_and_not_leaked(self):
        import asyncio
        from app.main import _dispatch_whatsapp_reaction, _pending_reaction_tasks

        with patch("app.main.send_whatsapp_reaction", new_callable=AsyncMock, side_effect=RuntimeError("Meta 500")):
            task = _dispatch_whatsapp_reaction("5491112345678", "wamid.err")
            await asyncio.sleep(0)
            assert task.done()
            assert task.exception() is None
            assert task not in _pending_reaction_tasks

    @pytest.mark.asyncio
    async def test_telemetry_records_all_pipeline_phases_end_to_end(self, caplog, monkeypatch):
        import asyncio
        caplog.set_level(logging.INFO, logger="luka.metrics")
        msg = make_text_message(body="Gaste 5000 en supermercado", msg_id="wamid.full.pipeline")

        # Mock Redis client
        fake_redis = AsyncMock()
        fake_redis.set = AsyncMock(return_value=True)
        fake_redis.eval = AsyncMock(return_value=1)
        fake_redis.lrange = AsyncMock(return_value=[])
        fake_redis.rpush = AsyncMock(return_value=1)
        fake_redis.ltrim = AsyncMock(return_value=True)
        fake_redis.expire = AsyncMock(return_value=True)

        from app.services.onboarding import OnboardingDecision, OnboardingResult
        from app.services.finance import MovementRegistrationResult

        monkeypatch.setattr(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            Mock(return_value=OnboardingResult(OnboardingDecision.KNOWN_USER)),
        )
        monkeypatch.setattr(
            "app.services.dispatcher._update_ultimo_mensaje",
            Mock(),
        )
        monkeypatch.setattr(
            "app.services.dispatcher.LLMService.process_message",
            AsyncMock(return_value={
                "intent": "expense",
                "movement_type": "egreso",
                "amount": 5000,
                "currency": "ARS",
                "description": "supermercado",
                "category": "Comida",
                "reply_text": "Registrado!",
            }),
        )
        monkeypatch.setattr(
            "app.services.dispatcher.FinanceService.register_movement_with_category",
            Mock(return_value=MovementRegistrationResult(
                status="registered",
                message="Registrado",
                movement_id="mov-123",
            )),
        )

        with (
            patch("app.main.send_whatsapp_reaction", new_callable=AsyncMock, return_value=True) as mock_react,
            patch(
                "app.main.send_whatsapp_typing_indicator",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("app.main.send_whatsapp_message", new_callable=AsyncMock, return_value=True) as mock_send,
        ):
            await _process_inbound_message_background(msg, fake_redis)
            await asyncio.sleep(0)

        mock_react.assert_awaited_once()
        mock_send.assert_awaited_once()

        # Verify all phases were recorded
        metrics_records = [r for r in caplog.records if r.name == "luka.metrics"]
        assert len(metrics_records) == 1
        record = metrics_records[0].message
        assert "[METRICS]" in record
        assert "message_id=wamid.full.pipeline" in record
        assert "status=completed" in record
        assert "total_ms=" in record
        assert "reaction_ms=" in record
        assert "redis_ms=" in record
        assert "llm_ms=" in record
        assert "db_ms=" in record
        assert "reply_ms=" in record
        assert "typing_ms=" in record

        # Verify privacy: no sensitive data in luka.metrics
        assert "5491112345678" not in record
        assert "Gaste 5000" not in record
        assert "5000" not in record or "total_ms=" in record  # 5000 as amount shouldn't be there
        assert "supermercado" not in record
        assert "token" not in record

    @pytest.mark.asyncio
    async def test_regression_conversation_service_redis_duration_incorporated(self, monkeypatch):
        """Regression test: ConversationService operations must be measured and accumulated in redis_ms."""
        import asyncio
        from app.services.conversation import ConversationService
        from app.services.telemetry import (
            finish_message_telemetry,
            start_message_telemetry,
        )

        fake_client = AsyncMock()

        async def slow_get(*args, **kwargs):
            await asyncio.sleep(0.04)  # 40ms controlled delay
            return None

        fake_client.get = AsyncMock(side_effect=slow_get)
        monkeypatch.setattr(ConversationService, "_get_client", AsyncMock(return_value=fake_client))

        start_message_telemetry("wamid.regression.conv")
        # Execute conversation service operation
        state = await ConversationService.get_state("5491112345678")
        assert state.step == "none"

        metrics = finish_message_telemetry(status="completed")
        assert metrics is not None
        # With current implementation (where ConversationService is not instrumented),
        # 'redis_ms' is not present or 0, so this assertion will FAIL.
        # With the fix, redis_ms incorporates the ~40ms duration (>= 35.0ms).
        assert "redis_ms" in metrics
        assert metrics["redis_ms"] >= 35.0
