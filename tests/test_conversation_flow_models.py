import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, ConversationFlow, ConversationFlowVersion


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine)()
    try:
        yield testing_session
    finally:
        testing_session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def create_flow(session, **overrides):
    unique = uuid.uuid4().hex
    values = {
        "slug": f"flow-{unique}",
        "name": "Flujo de prueba",
        "event_key": f"test.event.{unique}",
    }
    values.update(overrides)
    flow = ConversationFlow(**values)
    session.add(flow)
    session.flush()
    return flow


def create_version(session, flow, **overrides):
    values = {
        "flow_id": flow.id,
        "version_number": 1,
        "status": "draft",
        "definition": {"start_node": "start", "nodes": []},
    }
    values.update(overrides)
    version = ConversationFlowVersion(**values)
    session.add(version)
    return version


@pytest.mark.parametrize("field", ["slug", "name", "event_key"])
@pytest.mark.parametrize("value", ["", "   "])
def test_flow_identifiers_cannot_be_empty(session, field, value):
    with pytest.raises(IntegrityError):
        create_flow(session, **{field: value})


@pytest.mark.parametrize("field", ["slug", "event_key"])
def test_flow_identifiers_are_unique(session, field):
    repeated = f"same-{uuid.uuid4().hex}"
    create_flow(session, **{field: repeated})

    with pytest.raises(IntegrityError):
        create_flow(session, **{field: repeated})


def test_only_one_draft_version_is_allowed_per_flow(session):
    flow = create_flow(session)
    create_version(session, flow, version_number=1)
    create_version(session, flow, version_number=2)

    with pytest.raises(IntegrityError):
        session.commit()


def test_only_one_published_version_is_allowed_per_flow(session):
    flow = create_flow(session)
    published_at = datetime.now(timezone.utc)
    create_version(
        session,
        flow,
        version_number=1,
        status="published",
        published_at=published_at,
    )
    create_version(
        session,
        flow,
        version_number=2,
        status="published",
        published_at=published_at,
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_published_and_draft_versions_can_coexist(session):
    flow = create_flow(session)
    create_version(
        session,
        flow,
        version_number=1,
        status="published",
        published_at=datetime.now(timezone.utc),
    )
    create_version(session, flow, version_number=2)

    session.commit()


@pytest.mark.parametrize(
    ("status", "published_at"),
    [
        ("draft", datetime.now(timezone.utc)),
        ("published", None),
        ("retired", None),
    ],
)
def test_version_status_requires_consistent_publication_date(
    session, status, published_at
):
    flow = create_flow(session)
    create_version(session, flow, status=status, published_at=published_at)

    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_flow_deletes_its_versions(session):
    flow = create_flow(session)
    create_version(session, flow)
    session.commit()
    flow_id = flow.id

    session.delete(flow)
    session.commit()

    assert (
        session.query(ConversationFlowVersion)
        .filter(ConversationFlowVersion.flow_id == flow_id)
        .count()
        == 0
    )
