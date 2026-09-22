import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.whatsapp import WhatsAppReplyButtons, WhatsAppText
from app.models.database import Base
from app.services import conversation_flow
from app.services.conversation import (
    ConversationService,
    ConversationStateUnavailable,
)
from app.services.conversation_flow import ConversationFlowService
from app.services.conversation_flow_runtime import (
    INTERACTION_UNAVAILABLE_REPLY,
    ConfiguredFlowReply,
    ConversationFlowRuntime,
)


@pytest.fixture
def session_factory(monkeypatch):
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
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def flow_state(monkeypatch):
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
    return states


def create_and_publish(session_factory, *, event_key, definition):
    unique = uuid.uuid4().hex
    flow = ConversationFlowService.create(
        slug=f"flow-{unique}",
        name="Flujo runtime",
        event_key=event_key,
        definition=definition,
        session_factory=session_factory,
    )
    return ConversationFlowService.publish(
        flow.id,
        session_factory=session_factory,
    )


def terminal_definition(body):
    return {
        "start_node": "done",
        "nodes": [
            {"id": "done", "type": "text", "body": body, "terminal": True}
        ],
    }


def navigation_definition(final_body):
    return {
        "start_node": "question",
        "nodes": [
            {
                "id": "question",
                "type": "reply_button",
                "body": "¿Usamos {category}?",
                "options": [
                    {
                        "id": "details",
                        "title": "Ver detalle",
                        "next_node": "detail",
                    }
                ],
            },
            {
                "id": "detail",
                "type": "text",
                "body": final_body,
                "terminal": True,
            },
        ],
    }


@pytest.mark.asyncio
async def test_terminal_event_renders_without_pending_state(session_factory, flow_state):
    create_and_publish(
        session_factory,
        event_key="dashboard.link.sent",
        definition=terminal_definition("Entrá en {login_url}"),
    )

    message = await ConversationFlowRuntime.render_event(
        sender_phone="5411",
        event_key="dashboard.link.sent",
        variables={"login_url": "https://example.com/safe", "ttl_minutes": 10},
    )

    assert message == WhatsAppText("Entrá en https://example.com/safe")
    assert flow_state == {}


@pytest.mark.asyncio
async def test_interactive_event_stores_pinned_version(session_factory, flow_state):
    published = create_and_publish(
        session_factory,
        event_key="category.confirmation_required",
        definition=navigation_definition("Detalle de {category}"),
    )

    message = await ConversationFlowRuntime.render_event(
        sender_phone="5411",
        event_key="category.confirmation_required",
        variables={"category": "Agua"},
    )

    assert isinstance(message, WhatsAppReplyButtons)
    assert message.body == "¿Usamos Agua?"
    assert message.buttons[0].id.startswith("cf.")
    pending = flow_state["5411"]
    assert pending.flow_id == str(published.id)
    assert pending.version_id == str(published.published.id)
    assert pending.node_id == "question"


@pytest.mark.asyncio
async def test_active_conversation_keeps_original_version(session_factory, flow_state):
    published_v1 = create_and_publish(
        session_factory,
        event_key="category.confirmation_required",
        definition=navigation_definition("Versión uno: {category}"),
    )
    initial = await ConversationFlowRuntime.render_event(
        sender_phone="5411",
        event_key="category.confirmation_required",
        variables={"category": "Agua"},
    )
    old_option_id = initial.buttons[0].id
    ConversationFlowService.save_draft(
        published_v1.id,
        definition=navigation_definition("Versión dos: {category}"),
        session_factory=session_factory,
    )
    ConversationFlowService.publish(
        published_v1.id,
        session_factory=session_factory,
    )

    result = await ConversationFlowRuntime.handle_reply(
        sender_phone="5411",
        option_id=old_option_id,
        reply_type="button_reply",
        action_handler=AsyncMock(),
    )

    assert result.reply_message == WhatsAppText("Versión uno: Agua")
    assert "5411" not in flow_state


@pytest.mark.asyncio
async def test_unknown_option_does_not_execute_action(session_factory, flow_state):
    create_and_publish(
        session_factory,
        event_key="category.confirmation_required",
        definition={
            "start_node": "question",
            "nodes": [
                {
                    "id": "question",
                    "type": "reply_button",
                    "body": "¿Confirmás {category}?",
                    "options": [
                        {
                            "id": "confirm",
                            "title": "Confirmar",
                            "action": "confirm_category",
                        }
                    ],
                }
            ],
        },
    )
    await ConversationFlowRuntime.render_event(
        sender_phone="5411",
        event_key="category.confirmation_required",
        variables={"category": "Agua"},
    )
    action_handler = AsyncMock()

    result = await ConversationFlowRuntime.handle_reply(
        sender_phone="5411",
        option_id="cf.manipulated",
        reply_type="button_reply",
        action_handler=action_handler,
    )

    assert result == ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)
    action_handler.assert_not_awaited()
    assert "5411" in flow_state


@pytest.mark.asyncio
async def test_valid_action_clears_state_before_execution(session_factory, flow_state):
    create_and_publish(
        session_factory,
        event_key="category.confirmation_required",
        definition={
            "start_node": "question",
            "nodes": [
                {
                    "id": "question",
                    "type": "reply_button",
                    "body": "¿Confirmás {category}?",
                    "options": [
                        {
                            "id": "confirm",
                            "title": "Confirmar",
                            "action": "confirm_category",
                        }
                    ],
                }
            ],
        },
    )
    message = await ConversationFlowRuntime.render_event(
        sender_phone="5411",
        event_key="category.confirmation_required",
        variables={"category": "Agua"},
    )

    async def action_handler(action, variables):
        assert "5411" not in flow_state
        assert action == "confirm_category"
        assert variables == {"category": "Agua"}
        return ConfiguredFlowReply(reply_text="Confirmado")

    result = await ConversationFlowRuntime.handle_reply(
        sender_phone="5411",
        option_id=message.buttons[0].id,
        reply_type="button_reply",
        action_handler=action_handler,
    )

    assert result.reply_text == "Confirmado"


@pytest.mark.asyncio
async def test_redis_failure_falls_back_without_interactive_message(
    session_factory,
    monkeypatch,
):
    create_and_publish(
        session_factory,
        event_key="category.confirmation_required",
        definition=navigation_definition("Detalle"),
    )
    monkeypatch.setattr(
        ConversationService,
        "set_pending_conversation_flow",
        AsyncMock(side_effect=ConversationStateUnavailable("redis down")),
    )

    message = await ConversationFlowRuntime.render_event(
        sender_phone="5411",
        event_key="category.confirmation_required",
        variables={"category": "Agua"},
    )

    assert message is None


@pytest.mark.asyncio
async def test_missing_state_rejects_reply_without_action(flow_state):
    action_handler = AsyncMock()

    result = await ConversationFlowRuntime.handle_reply(
        sender_phone="5411",
        option_id="cf.any",
        reply_type="button_reply",
        action_handler=action_handler,
    )

    assert result.reply_text == INTERACTION_UNAVAILABLE_REPLY
    action_handler.assert_not_awaited()
