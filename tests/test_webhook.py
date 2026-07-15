import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.finance import MovementRegistrationResult
from app.services.onboarding import OnboardingDecision, OnboardingResult, OnboardingService


client = TestClient(app)


def make_text_message(body="Gaste 5000 en supermercado"):
    return {
        "from": "12345",
        "id": "wamid.HBgL",
        "timestamp": "1603059201",
        "text": {"body": body},
        "type": "text",
    }


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


def movement_llm_result(**overrides):
    result = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 5000,
        "currency": "ARS",
        "description": "supermercado",
        "expense": "supermercado",
        "reply_text": "LLM dice registrado, pero no debe usarse.",
    }
    result.update(overrides)
    return result


def registration_result(status):
    return MovementRegistrationResult(
        status=status,
        message=status,
        movement_id="movement-1" if status in {"registered", "duplicate"} else None,
        user_id="user-1",
        duplicate=status == "duplicate",
    )


def known_user_result():
    return OnboardingResult(OnboardingDecision.KNOWN_USER)


def post_webhook_with_mocks(llm_result, finance_result=None, messages=None):
    messages = messages if messages is not None else [make_text_message()]
    payload = make_webhook_payload(messages=messages)

    with (
        patch(
            "app.main.OnboardingService.prepare_whatsapp_message",
            return_value=known_user_result(),
        ),
        patch("app.main.LLMService.process_message", new_callable=AsyncMock) as process_message,
        patch(
            "app.main.FinanceService.register_movement_from_whatsapp_text",
            autospec=True,
        ) as register_movement,
        patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
    ):
        process_message.return_value = llm_result
        if finance_result is not None:
            register_movement.return_value = finance_result

        response = client.post("/webhook", json=payload)

    return response, process_message, register_movement, send_message


def test_verify_webhook_success():
    os.environ["WHATSAPP_VERIFY_TOKEN"] = "test_verify_token"

    from app.main import VERIFY_TOKEN

    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )

    assert response.status_code == 200
    assert response.text == "1158201444"


def test_verify_webhook_failure():
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "1158201444",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Verification failed"}


def test_handle_webhook_registered_movement_confirms_after_persistence():
    llm_result = movement_llm_result(reply_text="No uses esta confirmacion del LLM")
    finance_result = registration_result("registered")

    response, process_message, register_movement, send_message = post_webhook_with_mocks(
        llm_result=llm_result,
        finance_result=finance_result,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    process_message.assert_awaited_once_with("Gaste 5000 en supermercado")
    register_movement.assert_called_once_with(
        sender_phone="12345",
        whatsapp_message_id="wamid.HBgL",
        original_text="Gaste 5000 en supermercado",
        llm_result=llm_result,
    )
    send_message.assert_awaited_once_with(
        "12345",
        "✅ Registré tu egreso: supermercado por $5000 ARS.",
    )


def test_handle_webhook_registered_movement_does_not_use_llm_reply_text():
    llm_result = movement_llm_result(reply_text="✅ Ya lo registré antes de guardar")

    _, _, _, send_message = post_webhook_with_mocks(
        llm_result=llm_result,
        finance_result=registration_result("registered"),
    )

    sent_text = send_message.await_args.args[1]
    assert sent_text != llm_result["reply_text"]
    assert sent_text.startswith("✅ Registré tu egreso")


def test_handle_webhook_registered_income_movement_confirms_income():
    llm_result = movement_llm_result(
        movement_type="ingreso",
        amount=250000,
        description="sueldo",
        expense="sueldo",
    )

    _, _, register_movement, send_message = post_webhook_with_mocks(
        llm_result=llm_result,
        finance_result=registration_result("registered"),
    )

    register_movement.assert_called_once()
    sent_text = send_message.await_args.args[1]
    assert "ingreso: sueldo" in sent_text
    assert "$250000 ARS" in sent_text


def test_handle_webhook_duplicate_movement():
    response, _, register_movement, send_message = post_webhook_with_mocks(
        llm_result=movement_llm_result(),
        finance_result=registration_result("duplicate"),
    )

    assert response.status_code == 200
    register_movement.assert_called_once()
    send_message.assert_awaited_once_with(
        "12345",
        "Este movimiento ya había sido registrado, no lo dupliqué.",
    )


def test_handle_webhook_user_not_found():
    _, _, _, send_message = post_webhook_with_mocks(
        llm_result=movement_llm_result(),
        finance_result=registration_result("user_not_found"),
    )

    sent_text = send_message.await_args.args[1]
    assert "No encontré una cuenta vinculada" in sent_text
    assert "No pude registrar" in sent_text


def test_handle_webhook_invalid_data_requests_clarification():
    _, _, _, send_message = post_webhook_with_mocks(
        llm_result=movement_llm_result(),
        finance_result=registration_result("invalid_data"),
    )

    sent_text = send_message.await_args.args[1]
    assert "faltan datos claros" in sent_text
    assert "monto" in sent_text


def test_handle_webhook_persistence_error():
    _, _, _, send_message = post_webhook_with_mocks(
        llm_result=movement_llm_result(),
        finance_result=registration_result("persistence_error"),
    )

    sent_text = send_message.await_args.args[1]
    assert "problema registrando" in sent_text


def test_handle_webhook_greeting_does_not_call_finance_service():
    llm_result = {
        "intent": "greeting",
        "reply_text": "Hola, soy LUKA.",
    }

    response, process_message, register_movement, send_message = post_webhook_with_mocks(
        llm_result=llm_result,
    )

    assert response.status_code == 200
    process_message.assert_awaited_once()
    register_movement.assert_not_called()
    send_message.assert_awaited_once_with("12345", "Hola, soy LUKA.")


def test_handle_webhook_greeting_with_accidental_movement_type_does_not_call_finance_service():
    llm_result = {
        "intent": "greeting",
        "movement_type": "egreso",
        "amount": 5000,
        "reply_text": "Hola, soy LUKA.",
    }

    _, _, register_movement, send_message = post_webhook_with_mocks(llm_result=llm_result)

    register_movement.assert_not_called()
    send_message.assert_awaited_once_with("12345", "Hola, soy LUKA.")


def test_handle_webhook_out_of_scope_does_not_call_finance_service():
    llm_result = {
        "intent": "out_of_scope",
        "reply_text": "Solo puedo ayudarte con finanzas personales.",
    }

    _, _, register_movement, send_message = post_webhook_with_mocks(llm_result=llm_result)

    register_movement.assert_not_called()
    send_message.assert_awaited_once_with(
        "12345",
        "Solo puedo ayudarte con finanzas personales.",
    )


def test_handle_webhook_unavailable_intents_do_not_call_finance_service():
    for intent in ("reminder", "budget_query", "expense_summary"):
        llm_result = {
            "intent": intent,
            "reply_text": "✅ Acción realizada",
        }

        _, _, register_movement, send_message = post_webhook_with_mocks(llm_result=llm_result)

        register_movement.assert_not_called()
        sent_text = send_message.await_args.args[1]
        assert "todavía no está disponible" in sent_text
        assert "registrar ingresos y egresos" in sent_text


def test_handle_webhook_reminder_with_accidental_movement_type_does_not_call_finance_service():
    llm_result = {
        "intent": "reminder",
        "movement_type": "egreso",
        "amount": 5000,
        "reply_text": "Accion realizada",
    }

    _, _, register_movement, send_message = post_webhook_with_mocks(llm_result=llm_result)

    register_movement.assert_not_called()
    sent_text = send_message.await_args.args[1]
    assert "disponible" in sent_text
    assert "registrar ingresos y egresos" in sent_text


def test_handle_webhook_budget_query_with_accidental_movement_type_does_not_call_finance_service():
    llm_result = {
        "intent": "budget_query",
        "movement_type": "egreso",
        "amount": 5000,
        "reply_text": "Te queda presupuesto",
    }

    _, _, register_movement, send_message = post_webhook_with_mocks(llm_result=llm_result)

    register_movement.assert_not_called()
    sent_text = send_message.await_args.args[1]
    assert "disponible" in sent_text
    assert "registrar ingresos y egresos" in sent_text


def test_handle_webhook_not_a_movement_uses_backend_safe_text():
    llm_result = movement_llm_result(reply_text="LLM fallback no debe usarse")

    _, _, register_movement, send_message = post_webhook_with_mocks(
        llm_result=llm_result,
        finance_result=registration_result("not_a_movement"),
    )

    register_movement.assert_called_once()
    sent_text = send_message.await_args.args[1]
    assert sent_text != llm_result["reply_text"]
    assert "No identifiqu" in sent_text
    assert "supermercado" in sent_text


def test_handle_webhook_status_update_without_messages_does_not_fail():
    payload = make_webhook_payload(
        statuses=[
            {
                "id": "wamid.status",
                "status": "delivered",
            }
        ]
    )

    with (
        patch("app.main.LLMService.process_message", new_callable=AsyncMock) as process_message,
        patch(
            "app.main.FinanceService.register_movement_from_whatsapp_text",
            autospec=True,
        ) as register_movement,
        patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
    ):
        response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    process_message.assert_not_awaited()
    register_movement.assert_not_called()
    send_message.assert_not_awaited()


def test_unknown_user_sends_one_onboarding_message_without_calling_services():
    registration_url = "https://example.com/registro?token=safe-token"
    payload = make_webhook_payload(messages=[make_text_message()])
    onboarding_result = OnboardingResult(
        OnboardingDecision.SEND_INVITATION,
        registration_url=registration_url,
        invitation_ttl_minutes=30,
    )

    with (
        patch(
            "app.main.OnboardingService.prepare_whatsapp_message",
            return_value=onboarding_result,
        ) as prepare_onboarding,
        patch("app.main.LLMService.process_message", new_callable=AsyncMock) as process_message,
        patch(
            "app.main.FinanceService.register_movement_from_whatsapp_text",
            autospec=True,
        ) as register_movement,
        patch(
            "app.main.ReminderService.create_reminder",
            autospec=True,
        ) as create_reminder,
        patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
        patch("app.main._update_ultimo_mensaje") as update_last_message,
    ):
        response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    prepare_onboarding.assert_called_once_with("12345")
    process_message.assert_not_awaited()
    register_movement.assert_not_called()
    create_reminder.assert_not_called()
    update_last_message.assert_not_called()
    send_message.assert_awaited_once_with(
        "12345",
        "Para usar Luka, primero registrate y vinculá este WhatsApp:\n\n"
        f"{registration_url}\n\n"
        "El enlace vence en 30 minutos.",
    )


@pytest.mark.parametrize(
    "decision",
    [OnboardingDecision.SUPPRESS_RESPONSE],
)
def test_unknown_user_suppression_does_not_call_llm_or_send_message(decision):
    payload = make_webhook_payload(messages=[make_text_message()])

    with (
        patch(
            "app.main.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(decision),
        ),
        patch("app.main.LLMService.process_message", new_callable=AsyncMock) as process_message,
        patch(
            "app.main.FinanceService.register_movement_from_whatsapp_text",
            autospec=True,
        ) as register_movement,
        patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
    ):
        response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    process_message.assert_not_awaited()
    register_movement.assert_not_called()
    send_message.assert_not_awaited()


def test_onboarding_database_error_does_not_reach_llm():
    payload = make_webhook_payload(messages=[make_text_message()])

    with (
        patch(
            "app.main.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(OnboardingDecision.ERROR),
        ),
        patch("app.main.LLMService.process_message", new_callable=AsyncMock) as process_message,
        patch(
            "app.main.FinanceService.register_movement_from_whatsapp_text",
            autospec=True,
        ) as register_movement,
        patch(
            "app.main.ReminderService.create_reminder",
            autospec=True,
        ) as create_reminder,
        patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
    ):
        response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    process_message.assert_not_awaited()
    register_movement.assert_not_called()
    create_reminder.assert_not_called()
    send_message.assert_awaited_once_with(
        "12345",
        "No pude verificar tu cuenta. Intentá nuevamente en unos minutos.",
    )


class TestWebhookCreateReminder:
    """Tests for create_reminder intent routing and last message update in webhook."""

    @pytest.mark.asyncio
    async def test_create_reminder_via_webhook(self, monkeypatch):
        llm_response = {
            "intent": "create_reminder",
            "reminder_concept": "luz",
            "reminder_day": 15,
            "reminder_amount": None,
            "reminder_currency": None,
            "reply_text": "Estoy procesando el recordatorio.",
        }

        # Mock LLMService
        async def mock_process(text):
            return llm_response
        monkeypatch.setattr("app.main.LLMService.process_message", mock_process)
        monkeypatch.setattr(
            OnboardingService,
            "prepare_whatsapp_message",
            lambda phone: known_user_result(),
        )

        # Mock ReminderService
        from app.services.reminder import ReminderResult, ReminderService
        def mock_create(sender_phone, llm_result):
            return ReminderResult(status="created", message="ok", reminder_id="abc-123")
        monkeypatch.setattr(ReminderService, "create_reminder", mock_create)

        # Mock WhatsApp send
        sent_messages = []
        async def mock_send(to, text):
            sent_messages.append((to, text))
        monkeypatch.setattr("app.main.send_whatsapp_message", mock_send)

        # Mock update_ultimo_mensaje as no-op for simplicity in webhook route test
        monkeypatch.setattr("app.main._update_ultimo_mensaje", lambda phone: None)

        payload = make_webhook_payload(messages=[make_text_message("recordame pagar la luz el 15")])
        response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert len(sent_messages) == 1
        assert "luz" in sent_messages[0][1].lower()
        assert "15" in sent_messages[0][1]

    @pytest.mark.asyncio
    async def test_webhook_updates_ultimo_mensaje_en(self, monkeypatch):
        """Webhook updates usuario.ultimo_mensaje_en on any message."""
        llm_response = {
            "intent": "greeting",
            "reply_text": "Hola.",
        }

        async def mock_process(text):
            return llm_response
        monkeypatch.setattr("app.main.LLMService.process_message", mock_process)
        monkeypatch.setattr(
            OnboardingService,
            "prepare_whatsapp_message",
            lambda phone: known_user_result(),
        )
        monkeypatch.setattr("app.main.send_whatsapp_message", AsyncMock())

        # Track if _update_ultimo_mensaje was called
        calls = []
        def mock_update(phone):
            calls.append(phone)
        monkeypatch.setattr("app.main._update_ultimo_mensaje", mock_update)

        payload = make_webhook_payload(messages=[make_text_message("hola bot")])
        response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        assert len(calls) == 1
        assert calls[0] == "12345"
