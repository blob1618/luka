import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, ConversationFlowVersion
from app.services.conversation_flow import (
    ConversationFlowConflict,
    ConversationFlowNotFound,
    ConversationFlowService,
)


@pytest.fixture
def session_factory():
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
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def definition(body):
    return {
        "start_node": "start",
        "nodes": [{"id": "start", "type": "text", "body": body, "terminal": True}],
    }


def create_flow(session_factory, **overrides):
    unique = uuid.uuid4().hex
    values = {
        "slug": f"flow-{unique}",
        "name": "Flujo de prueba",
        "event_key": f"test.event.{unique}",
        "definition": definition("Borrador 1"),
        "session_factory": session_factory,
    }
    values.update(overrides)
    return ConversationFlowService.create(**values)


def test_create_starts_with_draft_version_one(session_factory):
    flow = create_flow(session_factory)

    assert flow.status == "active"
    assert flow.draft.version_number == 1
    assert flow.draft.definition == definition("Borrador 1")
    assert flow.published is None


def test_slug_and_event_are_unique(session_factory):
    flow = create_flow(session_factory)

    with pytest.raises(ConversationFlowConflict):
        create_flow(
            session_factory,
            slug=flow.slug,
            event_key=f"other.event.{uuid.uuid4().hex}",
        )


def test_publish_then_edit_creates_new_draft_without_mutating_published(
    session_factory,
):
    flow = create_flow(session_factory)
    published_at = datetime(2026, 9, 16, tzinfo=timezone.utc)

    published = ConversationFlowService.publish(
        flow.id,
        session_factory=session_factory,
        now=published_at,
    )
    edited = ConversationFlowService.save_draft(
        flow.id,
        definition=definition("Borrador 2"),
        session_factory=session_factory,
    )

    assert published.draft is None
    assert edited.published.version_number == 1
    assert edited.published.definition == definition("Borrador 1")
    assert edited.published.published_at == published_at
    assert edited.draft.version_number == 2
    assert edited.draft.definition == definition("Borrador 2")


def test_republish_retires_previous_version(session_factory):
    flow = create_flow(session_factory)
    ConversationFlowService.publish(flow.id, session_factory=session_factory)
    ConversationFlowService.save_draft(
        flow.id,
        definition=definition("Version 2"),
        session_factory=session_factory,
    )

    current = ConversationFlowService.publish(
        flow.id,
        session_factory=session_factory,
    )

    assert current.published.version_number == 2
    session = session_factory()
    try:
        retired = (
            session.query(ConversationFlowVersion)
            .filter(
                ConversationFlowVersion.flow_id == flow.id,
                ConversationFlowVersion.status == "retired",
            )
            .one()
        )
        assert retired.version_number == 1
        assert retired.definition == definition("Borrador 1")
    finally:
        session.close()


def test_archive_stops_editing_and_publishing(session_factory):
    flow = create_flow(session_factory)
    archived = ConversationFlowService.archive(
        flow.id,
        session_factory=session_factory,
    )

    assert archived.status == "archived"
    with pytest.raises(ConversationFlowConflict):
        ConversationFlowService.save_draft(
            flow.id,
            definition=definition("Cambio"),
            session_factory=session_factory,
        )
    with pytest.raises(ConversationFlowConflict):
        ConversationFlowService.publish(flow.id, session_factory=session_factory)


def test_unknown_flow_is_reported(session_factory):
    with pytest.raises(ConversationFlowNotFound):
        ConversationFlowService.get(uuid.uuid4(), session_factory=session_factory)


def test_list_returns_created_flows(session_factory):
    first = create_flow(session_factory, name="A")
    second = create_flow(session_factory, name="B")

    flows = ConversationFlowService.list(session_factory=session_factory)

    assert [flow.id for flow in flows] == [first.id, second.id]
