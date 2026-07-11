import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
from app.main import app
from app.models.database import Base, Categoria, MovimientoFinanciero, Usuario


client = TestClient(app)


@pytest.fixture()
def db_context(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(finance_module, "SessionLocal", testing_session_local)

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


def post_webhook_with_real_finance(payload, llm_result):
    with (
        patch("app.main.LLMService.process_message", new_callable=AsyncMock) as process_message,
        patch("app.main.send_whatsapp_message", new_callable=AsyncMock) as send_message,
    ):
        process_message.return_value = llm_result
        response = client.post("/webhook", json=payload)

    return response, process_message, send_message


def movements(session):
    return session.query(MovimientoFinanciero).all()


def test_webhook_integration_valid_expense_creates_egreso(db_context):
    session = db_context["session"]
    user = create_user(session)
    category = create_category(session, user.id)
    payload = make_webhook_payload()

    response, process_message, send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_movement_result(),
    )

    saved_movements = movements(session)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    process_message.assert_awaited_once_with("Gaste 5000 en supermercado")
    send_message.assert_awaited_once()
    assert "egreso: supermercado" in send_message.await_args.args[1]
    assert len(saved_movements) == 1
    movement = saved_movements[0]
    assert movement.usuario_id == user.id
    assert movement.categoria_id == category.id
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

    response, _, send_message = post_webhook_with_real_finance(
        payload=payload,
        llm_result=llm_movement_result(),
    )

    assert response.status_code == 200
    assert movements(session) == []
    assert "No encontr" in send_message.await_args.args[1]
    assert "No pude registrar" in send_message.await_args.args[1]


def test_webhook_integration_duplicate_message_id_does_not_create_second_row(db_context):
    session = db_context["session"]
    create_user(session)
    payload = make_webhook_payload(whatsapp_message_id="wamid.integration.duplicate")
    llm_result = llm_movement_result()

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
    assert "ya hab" in second_send_message.await_args.args[1]
    assert "no lo dupli" in second_send_message.await_args.args[1]
