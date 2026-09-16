from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import Base, ConversationFlow, ConversationFlowVersion
from app.services import conversation_flow


ADMIN_KEY = "test-flow-admin-key"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


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
    monkeypatch.setenv("FLOW_ADMIN_API_KEY", ADMIN_KEY)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def client(session_factory):
    return TestClient(app)


def definition(body="Registré {description}."):
    return {
        "start_node": "done",
        "nodes": [
            {"id": "done", "type": "text", "body": body, "terminal": True}
        ],
    }


def create_payload(**overrides):
    payload = {
        "slug": "movement-registered",
        "name": "Movimiento registrado",
        "event_key": "movement.registered",
        "definition": definition(),
    }
    payload.update(overrides)
    return payload


def create_flow(client, **overrides):
    response = client.post(
        "/admin/conversation-flows",
        headers=AUTH_HEADERS,
        json=create_payload(**overrides),
    )
    assert response.status_code == 201
    return response.json()


def test_admin_api_requires_configured_bearer_key(client, monkeypatch):
    response = client.get("/admin/conversation-flows")
    assert response.status_code == 401

    monkeypatch.delenv("FLOW_ADMIN_API_KEY")
    response = client.get("/admin/conversation-flows", headers=AUTH_HEADERS)
    assert response.status_code == 503


def test_contract_endpoint_exposes_events(client):
    response = client.get(
        "/admin/conversation-flows/contracts",
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert any(
        event["event_key"] == "movement.registered"
        for event in response.json()["events"]
    )


def test_create_list_and_get_flow(client):
    created = create_flow(client)

    listed = client.get("/admin/conversation-flows", headers=AUTH_HEADERS)
    detail = client.get(
        f"/admin/conversation-flows/{created['id']}",
        headers=AUTH_HEADERS,
    )

    assert listed.status_code == 200
    assert [flow["id"] for flow in listed.json()] == [created["id"]]
    assert detail.status_code == 200
    assert detail.json()["draft"]["version_number"] == 1
    assert len(detail.json()["versions"]) == 1


def test_invalid_flow_is_not_created(client, session_factory):
    response = client.post(
        "/admin/conversation-flows",
        headers=AUTH_HEADERS,
        json=create_payload(definition=definition("{not_allowed}")),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_flow_definition"
    session = session_factory()
    try:
        assert session.query(ConversationFlow).count() == 0
    finally:
        session.close()


def test_save_publish_and_archive(client):
    created = create_flow(client)
    updated_definition = definition("Guardé {amount} {currency}.")

    saved = client.put(
        f"/admin/conversation-flows/{created['id']}/draft",
        headers=AUTH_HEADERS,
        json={"name": "Confirmación de movimiento", "definition": updated_definition},
    )
    published = client.post(
        f"/admin/conversation-flows/{created['id']}/publish",
        headers=AUTH_HEADERS,
    )
    archived = client.post(
        f"/admin/conversation-flows/{created['id']}/archive",
        headers=AUTH_HEADERS,
    )

    assert saved.status_code == 200
    assert saved.json()["name"] == "Confirmación de movimiento"
    assert published.status_code == 200
    assert published.json()["draft"] is None
    assert published.json()["published"]["definition"] == updated_definition
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"


def test_publish_revalidates_stored_draft(client, session_factory):
    created = create_flow(client)
    session = session_factory()
    try:
        draft = (
            session.query(ConversationFlowVersion)
            .filter(ConversationFlowVersion.flow_id == UUID(created["id"]))
            .one()
        )
        invalid = deepcopy(draft.definition)
        invalid["nodes"][0]["body"] = "{not_allowed}"
        draft.definition = invalid
        session.commit()
    finally:
        session.close()

    response = client.post(
        f"/admin/conversation-flows/{created['id']}/publish",
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422
    session = session_factory()
    try:
        draft = session.query(ConversationFlowVersion).one()
        assert draft.status == "draft"
    finally:
        session.close()


def test_discard_draft_keeps_published_version(client):
    created = create_flow(client)
    client.post(
        f"/admin/conversation-flows/{created['id']}/publish",
        headers=AUTH_HEADERS,
    )
    client.put(
        f"/admin/conversation-flows/{created['id']}/draft",
        headers=AUTH_HEADERS,
        json={"definition": definition("Cambio {amount}")},
    )

    response = client.delete(
        f"/admin/conversation-flows/{created['id']}/draft",
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["draft"] is None
    assert response.json()["published"]["version_number"] == 1


def test_validate_endpoint_does_not_persist(client, session_factory):
    response = client.post(
        "/admin/conversation-flows/validate",
        headers=AUTH_HEADERS,
        json={"event_key": "movement.registered", "definition": definition()},
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
    session = session_factory()
    try:
        assert session.query(ConversationFlow).count() == 0
    finally:
        session.close()
