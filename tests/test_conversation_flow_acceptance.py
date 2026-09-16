import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.whatsapp import WhatsAppList, WhatsAppReplyButtons, WhatsAppText
from app.main import app
from app.models.database import Base
from app.services import conversation_flow
from app.services.conversation import ConversationService
from app.services.conversation_flow_runtime import ConversationFlowRuntime


ADMIN_KEY = "acceptance-admin-key"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def flow_environment(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(conversation_flow, "SessionLocal", factory)
    monkeypatch.setenv("FLOW_ADMIN_API_KEY", ADMIN_KEY)

    states = {}

    async def get_state(phone):
        return states.get(phone)

    async def set_state(phone, pending):
        states[phone] = pending

    async def clear_state(phone):
        states.pop(phone, None)

    monkeypatch.setattr(
        ConversationService,
        "get_pending_conversation_flow",
        get_state,
    )
    monkeypatch.setattr(
        ConversationService,
        "set_pending_conversation_flow",
        set_state,
    )
    monkeypatch.setattr(
        ConversationService,
        "clear_pending_conversation_flow",
        clear_state,
    )
    try:
        yield factory, states
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def button_definition():
    return {
        "start_node": "question",
        "nodes": [
            {
                "id": "question",
                "type": "reply_button",
                "body": "¿Querés ver el detalle de {category}?",
                "options": [
                    {
                        "id": "details",
                        "title": "Ver detalle",
                        "next_node": "done",
                    }
                ],
            },
            {
                "id": "done",
                "type": "text",
                "body": "Detalle de {category}",
                "terminal": True,
            },
        ],
    }


def list_definition():
    return {
        "start_node": "question",
        "nodes": [
            {
                "id": "question",
                "type": "list",
                "body": "Elegí qué querés ver de {category}",
                "button": "Ver opciones",
                "sections": [
                    {
                        "title": "Detalle",
                        "options": [
                            {
                                "id": "details",
                                "title": "Ver detalle",
                                "description": "Muestra la categoría",
                                "next_node": "done",
                            }
                        ],
                    }
                ],
            },
            {
                "id": "done",
                "type": "text",
                "body": "Detalle de {category}",
                "terminal": True,
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition_factory", "message_type", "reply_type", "option_getter"),
    [
        (
            button_definition,
            WhatsAppReplyButtons,
            "button_reply",
            lambda message: message.buttons[0].id,
        ),
        (
            list_definition,
            WhatsAppList,
            "list_reply",
            lambda message: message.sections[0].rows[0].id,
        ),
    ],
)
async def test_admin_publish_to_interactive_runtime_round_trip(
    flow_environment,
    definition_factory,
    message_type,
    reply_type,
    option_getter,
):
    _factory, states = flow_environment
    payload = {
        "slug": f"acceptance-{uuid.uuid4().hex}",
        "name": "Recorrido de aceptación",
        "event_key": "category.confirmation_required",
        "definition": definition_factory(),
    }

    client = TestClient(app)
    created = client.post(
        "/admin/conversation-flows",
        headers=AUTH_HEADERS,
        json=payload,
    )
    assert created.status_code == 201
    published = client.post(
        f"/admin/conversation-flows/{created.json()['id']}/publish",
        headers=AUTH_HEADERS,
    )
    assert published.status_code == 200

    first_message = await ConversationFlowRuntime.render_event(
        sender_phone="541100000001",
        event_key="category.confirmation_required",
        variables={"category": "Agua"},
    )

    assert isinstance(first_message, message_type)
    assert states["541100000001"].version_id == published.json()["published"]["id"]

    action_handler = AsyncMock()
    result = await ConversationFlowRuntime.handle_reply(
        sender_phone="541100000001",
        option_id=option_getter(first_message),
        reply_type=reply_type,
        action_handler=action_handler,
    )

    assert result.reply_message == WhatsAppText("Detalle de Agua")
    assert "541100000001" not in states
    action_handler.assert_not_awaited()
