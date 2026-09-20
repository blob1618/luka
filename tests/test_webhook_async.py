import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
