import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.database as database_module
import app.services.finance as finance_module
import app.services.onboarding as onboarding_module
import app.services.reminder as reminder_module
from app.main import app
from app.models.database import (
    Base,
    Categoria,
    MovimientoFinanciero,
    OnboardingInvitacion,
    Usuario,
)
from app.services.webhook_idempotency import InboundMessageClaim

pytestmark = pytest.mark.integration

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_pending_conversation_flows(monkeypatch):
    for method in (
        "is_awaiting_category_confirmation",
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


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(finance_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(onboarding_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(database_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(reminder_module, "SessionLocal", testing_session_local)
    monkeypatch.setenv(
        "ONBOARDING_REGISTRATION_URL",
        "https://example.com/registro",
    )
    monkeypatch.setenv("ONBOARDING_INVITATION_TTL_MINUTES", "30")
    monkeypatch.setenv("ONBOARDING_RESEND_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("ONBOARDING_MAX_RESENDS", "3")

    session = testing_session_local()
    try:
        yield {
            "session": session,
        }
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def make_webhook_payload(
    body="Gaste 5000 en supermercado",
    sender_phone="12345",
    whatsapp_message_id="wamid.integration.1",
):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456789",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "123456123456",
                            },
                            "contacts": [
                                {"profile": {"name": "Test User"}, "wa_id": sender_phone}
                            ],
                            "messages": [
                                {
                                    "from": sender_phone,
                                    "id": whatsapp_message_id,
                                    "timestamp": "1603059201",
                                    "text": {"body": body},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def llm_movement_result(**overrides):
    result = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 5000,
        "currency": "ARS",
        "description": "supermercado",
        "expense": "supermercado",
        "category": "supermercado",
        "reply_text": "LLM reply must not confirm persistence.",
    }
    result.update(overrides)
    return result


def create_user(session, whatsapp_id="12345"):
    user = Usuario(
        nombre="Integration User",
        email=f"{uuid.uuid4()}@example.com",
        whatsapp_id=whatsapp_id,
    )
    session.add(user)
    session.commit()
    return user


def create_category(session, user_id, nombre="supermercado"):
    category = Categoria(
        usuario_id=user_id,
        nombre=nombre,
        es_default=False,
        esta_eliminado=False,
    )
    session.add(category)
    session.commit()
    return category


def post_webhook_with_real_finance(payload, llm_result, expects_category_confirmation=False):
    with (
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock) as process_message,
        patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
        patch("app.services.dispatcher.ConversationService.is_awaiting_category_confirmation", new_callable=AsyncMock) as mock_is_awaiting,
        patch("app.services.dispatcher.ConversationService.get_pending_movement", new_callable=AsyncMock) as mock_get_pending,
    ):
        if expects_category_confirmation:
            mock_is_awaiting.return_value = True
            mock_get_pending.return_value = None
        else:
            mock_is_awaiting.return_value = False

        process_message.return_value = llm_result
        response = client.post("/webhook", json=payload)

    return response, process_message, send_message


def movements(session):
    return session.query(MovimientoFinanciero).all()


def onboarding_invitations(session):
    return session.query(OnboardingInvitacion).all()


def test_webhook_integration_valid_expense_creates_egreso(db_context):
    session = db_context["session"]
    user = create_user(session)
    payload = make_webhook_payload()

    response, process_message, send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_movement_result(category=None),
    )

    saved_movements = movements(session)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    process_message.assert_awaited_once()
    assert process_message.await_args.args[0] == "Gaste 5000 en supermercado"
    send_message.assert_awaited_once()
    assert "egreso: supermercado" in send_message.await_args.args[1]
    assert len(saved_movements) == 1
    movement = saved_movements[0]
    assert movement.usuario_id == user.id
    assert movement.categoria_id is None
    assert movement.tipo == "egreso"
    assert movement.cantidad == 5000
    assert movement.moneda == "ARS"
    assert movement.descripcion == "supermercado"
    assert movement.whatsapp_message_id == "wamid.integration.1"


def test_webhook_integration_valid_income_creates_ingreso(db_context):
    session = db_context["session"]
    user = create_user(session)
    payload = make_webhook_payload(
        body="Me pagaron el sueldo",
        whatsapp_message_id="wamid.integration.2",
    )

    response, _, send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_movement_result(
            movement_type="ingreso",
            amount=250000,
            description="sueldo",
            expense="sueldo",
            category=None,
        ),
    )

    saved_movements = movements(session)

    assert response.status_code == 200
    assert len(saved_movements) == 1
    movement = saved_movements[0]
    assert movement.usuario_id == user.id
    assert movement.categoria_id is None
    assert movement.tipo == "ingreso"
    assert movement.cantidad == 250000
    assert movement.descripcion == "sueldo"
    assert movement.whatsapp_message_id == "wamid.integration.2"
    assert "ingreso: sueldo" in send_message.await_args.args[1]


def test_webhook_integration_unknown_user_does_not_create_movement(db_context):
    session = db_context["session"]
    payload = make_webhook_payload(
        sender_phone="99999",
        whatsapp_message_id="wamid.integration.3",
    )

    response, process_message, send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_movement_result(),
    )

    assert response.status_code == 200
    process_message.assert_not_awaited()
    assert movements(session) == []
    invitations = onboarding_invitations(session)
    assert len(invitations) == 1
    assert invitations[0].whatsapp_id == "99999"
    assert invitations[0].estado == "pendiente"
    assert invitations[0].reenvios == 0
    sent_text = send_message.await_args.args[1]
    assert "Para usar Luka" in sent_text
    assert "https://example.com/registro?token=" in sent_text
    assert "99999" not in sent_text


def test_webhook_integration_duplicate_message_id_does_not_create_second_row(db_context):
    session = db_context["session"]
    create_user(session)
    payload = make_webhook_payload(whatsapp_message_id="wamid.integration.duplicate")
    llm_result = llm_movement_result(category=None)

    first_response, _, first_send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_result,
    )
    second_response, _, second_send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_result,
    )

    saved_movements = movements(session)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(saved_movements) == 1
    assert "egreso: supermercado" in first_send_message.await_args.args[1]
    second_send_message.assert_not_awaited()


def test_create_reminder_not_processed_twice(db_context):
    """Ensure create_reminder intent triggers send_whatsapp_message exactly once."""
    session = db_context["session"]
    create_user(session, whatsapp_id="5491155551234")
    payload = make_webhook_payload(
        body="Avisame del alquiler el dia 1",
        sender_phone="5491155551234",
        whatsapp_message_id="wamid.reminder.dedup.1",
    )
    llm_result = {
        "intent": "create_reminder",
        "movement_type": None,
        "reminder_concept": "alquiler",
        "reminder_day": 1,
        "reminder_amount": None,
        "reminder_currency": "ARS",
        "reply_text": "Estoy procesando el recordatorio.",
        "expense": None,
        "amount": None,
        "currency": None,
        "category": None,
        "description": None,
        "reminder_title": None,
        "reminder_date": None,
    }

    response, process_message, send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_result,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    process_message.assert_awaited_once()
    # send_whatsapp_message called exactly once (for the reply)
    assert send_message.await_count == 1


class TestReminderMultiTurn:
    @pytest.mark.asyncio
    async def test_missing_day_asks_then_completes(self, db_context):
        """Turn 1: 'avisame del cable' -> no day -> asks. Turn 2: 'el 10' -> completes."""
        session = db_context["session"]
        create_user(session, whatsapp_id="5491155551234")

        pending = None

        async def set_pending(phone, p):
            nonlocal pending
            pending = p

        async def get_pending(phone):
            return pending

        async def clear_state(phone):
            nonlocal pending
            pending = None

        turn1_payload = make_webhook_payload(
            body="avisame del cable",
            sender_phone="5491155551234",
            whatsapp_message_id="wamid.multiturn.1",
        )
        turn1_llm = {
            "intent": "create_reminder",
            "movement_type": None,
            "reminder_concept": "cable",
            "reminder_day": None,
            "reminder_amount": None,
            "reminder_currency": "ARS",
            "reply_text": "Estoy procesando el recordatorio.",
            "expense": None, "amount": None, "currency": None,
            "category": None, "description": None,
            "reminder_title": None, "reminder_date": None,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock) as process_message,
            patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
            patch("app.services.dispatcher.ConversationService.is_awaiting_category_confirmation", new_callable=AsyncMock) as mock_cat,
            patch("app.services.dispatcher.ConversationService.get_pending_movement", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.is_awaiting_reminder_data", new_callable=AsyncMock) as mock_await_rem,
            patch("app.services.dispatcher.ConversationService.is_awaiting_rename", new_callable=AsyncMock) as mock_await_rename,
            patch("app.services.dispatcher.ConversationService.get_pending_reminder", new_callable=AsyncMock) as mock_get_rem,
            patch("app.services.dispatcher.ConversationService.set_pending_reminder", new_callable=AsyncMock) as mock_set_rem,
            patch("app.services.dispatcher.ConversationService.set_pending_rename", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.clear_state", new_callable=AsyncMock) as mock_clear,
        ):
            mock_cat.return_value = False
            mock_await_rem.return_value = False
            mock_await_rename.return_value = False
            mock_set_rem.side_effect = set_pending
            mock_get_rem.side_effect = get_pending
            mock_clear.side_effect = clear_state
            process_message.return_value = turn1_llm

            response1 = client.post("/webhook", json=turn1_payload)

        assert response1.status_code == 200
        send_message.assert_awaited_once()
        turn1_reply = send_message.await_args.args[1]
        assert "día" in turn1_reply.lower() or "dia" in turn1_reply.lower()
        assert pending is not None
        assert pending.reminder_concept == "cable"

        # Turn 2: provide the day
        send_message.reset_mock()

        turn2_payload = make_webhook_payload(
            body="el 10",
            sender_phone="5491155551234",
            whatsapp_message_id="wamid.multiturn.2",
        )

        with (
            patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock) as process_message2,
            patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message2,
            patch("app.services.dispatcher.ConversationService.is_awaiting_category_confirmation", new_callable=AsyncMock) as mock_cat2,
            patch("app.services.dispatcher.ConversationService.get_pending_movement", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.is_awaiting_reminder_data", new_callable=AsyncMock) as mock_await_rem2,
            patch("app.services.dispatcher.ConversationService.is_awaiting_rename", new_callable=AsyncMock) as mock_await_rename2,
            patch("app.services.dispatcher.ConversationService.get_pending_reminder", new_callable=AsyncMock) as mock_get_rem2,
            patch("app.services.dispatcher.ConversationService.set_pending_reminder", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.set_pending_rename", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.clear_state", new_callable=AsyncMock) as mock_clear2,
        ):
            mock_cat2.return_value = False
            mock_await_rem2.return_value = True
            mock_get_rem2.return_value = pending
            mock_await_rename2.return_value = False
            mock_clear2.side_effect = clear_state
            process_message2.return_value = {"reminder_day": 10}

            response2 = client.post("/webhook", json=turn2_payload)

        assert response2.status_code == 200
        send_message2.assert_awaited_once()
        turn2_reply = send_message2.await_args.args[1]
        assert "Dale" in turn2_reply

        # Verify reminder created in DB
        from app.models.database import Recordatorio
        recs = session.query(Recordatorio).all()
        assert len(recs) == 1
        assert recs[0].titulo == "cable"
        assert recs[0].dia_del_mes == 10

    @pytest.mark.asyncio
    async def test_missing_concept_asks_name(self, db_context):
        """When concept cannot be extracted, bot asks for a name."""
        session = db_context["session"]
        create_user(session, whatsapp_id="5491155559999")

        payload = make_webhook_payload(
            body="recordame",
            sender_phone="5491155559999",
            whatsapp_message_id="wamid.multiturn.noconcept.1",
        )
        llm_result = {
            "intent": "create_reminder",
            "movement_type": None,
            "reminder_concept": None,
            "reminder_day": 5,
            "reminder_amount": None,
            "reminder_currency": "ARS",
            "reply_text": "Estoy procesando el recordatorio.",
            "expense": None, "amount": None, "currency": None,
            "category": None, "description": None,
            "reminder_title": None, "reminder_date": None,
        }

        with (
            patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock) as process_message,
            patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
            patch("app.services.dispatcher.ConversationService.is_awaiting_category_confirmation", new_callable=AsyncMock) as mock_cat,
            patch("app.services.dispatcher.ConversationService.get_pending_movement", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.is_awaiting_reminder_data", new_callable=AsyncMock) as mock_await_rem,
            patch("app.services.dispatcher.ConversationService.is_awaiting_rename", new_callable=AsyncMock) as mock_await_rename,
            patch("app.services.dispatcher.ConversationService.get_pending_reminder", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.set_pending_reminder", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.set_pending_rename", new_callable=AsyncMock),
            patch("app.services.dispatcher.ConversationService.clear_state", new_callable=AsyncMock),
        ):
            mock_cat.return_value = False
            mock_await_rem.return_value = False
            mock_await_rename.return_value = False
            process_message.return_value = llm_result

            response = client.post("/webhook", json=payload)

        assert response.status_code == 200
        send_message.assert_awaited_once()
        reply = send_message.await_args.args[1]
        assert "nombre" in reply.lower()
