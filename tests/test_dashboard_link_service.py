import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, DashboardLoginLink, Usuario
from app.services.dashboard_link import (
    DashboardLinkConfig,
    DashboardLinkDecision,
    DashboardLinkService,
)


PHONE = "5491100000000"
NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )
    try:
        yield session_factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def config():
    return DashboardLinkConfig(
        login_url="https://example.com/login",
        link_ttl_minutes=10,
        resend_cooldown_seconds=60,
        max_resends=3,
    )


def generate(database, config, *, now=NOW):
    return DashboardLinkService.generate_or_reuse(
        PHONE,
        session_factory=database,
        config=config,
        now=now,
    )


def links(database):
    with database() as session:
        return session.query(DashboardLoginLink).order_by(
            DashboardLoginLink.creado_en
        ).all()


def create_linked_user(database) -> uuid.UUID:
    with database() as session:
        user = Usuario(
            nombre="Usuario vinculado",
            email="linked@example.com",
            whatsapp_id=PHONE,
            auth_user_id=uuid.uuid4(),
        )
        session.add(user)
        session.commit()
        return user.id


def create_unlinked_user(database) -> None:
    with database() as session:
        session.add(
            Usuario(
                nombre="Usuario sin vincular",
                email="unlinked@example.com",
                whatsapp_id=PHONE,
                auth_user_id=None,
            )
        )
        session.commit()


def test_unknown_number_is_not_eligible(database, config):
    result = generate(database, config)

    assert result.decision == DashboardLinkDecision.NOT_ELIGIBLE
    assert links(database) == []


def test_known_but_unlinked_user_is_not_eligible(database, config):
    create_unlinked_user(database)

    result = generate(database, config)

    assert result.decision == DashboardLinkDecision.NOT_ELIGIBLE
    assert links(database) == []


def test_linked_user_gets_a_link_with_hashed_token(database, config, monkeypatch):
    raw_token = "token-original-super-secreto"
    monkeypatch.setattr(
        "app.services.dashboard_link.secrets.token_urlsafe", lambda _: raw_token
    )
    usuario_id = create_linked_user(database)

    result = generate(database, config)
    saved = links(database)

    assert result.decision == DashboardLinkDecision.SEND_LINK
    assert len(saved) == 1
    assert saved[0].usuario_id == usuario_id
    assert saved[0].estado == "pendiente"
    assert saved[0].token_hash != raw_token
    assert saved[0].token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert raw_token in result.login_url
    assert result.login_url.startswith("https://example.com/login?")


def test_link_url_carries_only_the_token(database, config):
    create_linked_user(database)

    result = generate(database, config)
    parsed = urlsplit(result.login_url)
    query = parse_qs(parsed.query)

    assert list(query.keys()) == ["token"]
    assert PHONE not in result.login_url


def test_pending_link_inside_cooldown_is_suppressed(database, config):
    create_linked_user(database)
    first_result = generate(database, config)
    before = links(database)[0]

    second_result = generate(database, config, now=NOW + timedelta(seconds=59))
    after = links(database)

    assert first_result.decision == DashboardLinkDecision.SEND_LINK
    assert second_result.decision == DashboardLinkDecision.SUPPRESS_RESPONSE
    assert len(after) == 1
    assert after[0].id == before.id
    assert after[0].token_hash == before.token_hash
    assert after[0].reenvios == 0


def test_resend_after_cooldown_rotates_token(database, config, monkeypatch):
    tokens = iter(("first-token", "second-token"))
    monkeypatch.setattr(
        "app.services.dashboard_link.secrets.token_urlsafe",
        lambda _: next(tokens),
    )
    create_linked_user(database)
    first_result = generate(database, config)
    before = links(database)[0]

    second_result = generate(database, config, now=NOW + timedelta(seconds=60))
    after = links(database)

    assert first_result.login_url != second_result.login_url
    assert second_result.decision == DashboardLinkDecision.SEND_LINK
    assert len(after) == 1
    assert after[0].id == before.id
    assert after[0].token_hash != before.token_hash
    assert after[0].reenvios == 1


def test_expired_link_is_closed_and_new_one_created(database, config):
    usuario_id = create_linked_user(database)
    old_created_at = NOW - timedelta(hours=1)
    old_link = DashboardLoginLink(
        usuario_id=usuario_id,
        token_hash=hashlib.sha256(b"expired-token").hexdigest(),
        estado="pendiente",
        expira_en=NOW - timedelta(seconds=1),
        reenvios=1,
        ultimo_envio_en=NOW - timedelta(minutes=11),
        creado_en=old_created_at,
        actualizado_en=old_created_at,
    )
    with database() as session:
        session.add(old_link)
        session.commit()
        old_id = old_link.id

    result = generate(database, config)
    saved = links(database)

    assert result.decision == DashboardLinkDecision.SEND_LINK
    assert len(saved) == 2
    expired = next(row for row in saved if row.id == old_id)
    pending = next(row for row in saved if row.id != old_id)
    assert expired.estado == "vencido"
    assert pending.estado == "pendiente"
    assert pending.reenvios == 0


def test_maximum_resends_suppresses_new_response(database, config, monkeypatch):
    tokens = iter(f"token-{index}" for index in range(4))
    monkeypatch.setattr(
        "app.services.dashboard_link.secrets.token_urlsafe",
        lambda _: next(tokens),
    )
    create_linked_user(database)
    generate(database, config)
    for resend_number in range(1, 4):
        result = generate(
            database, config, now=NOW + timedelta(seconds=60 * resend_number)
        )
        assert result.decision == DashboardLinkDecision.SEND_LINK

    before = links(database)[0]
    result = generate(database, config, now=NOW + timedelta(seconds=240))
    after = links(database)

    assert result.decision == DashboardLinkDecision.SUPPRESS_RESPONSE
    assert len(after) == 1
    assert after[0].id == before.id
    assert after[0].token_hash == before.token_hash
    assert after[0].reenvios == 3


def test_session_creation_error_returns_controlled_decision(config):
    def broken_session_factory():
        raise RuntimeError("boom")

    result = DashboardLinkService.generate_or_reuse(
        PHONE,
        session_factory=broken_session_factory,
        config=config,
        now=NOW,
    )

    assert result.decision == DashboardLinkDecision.ERROR


def test_generate_with_date_filters_builds_canonical_url(database, config):
    create_linked_user(database)
    result = DashboardLinkService.generate_or_reuse(
        PHONE,
        session_factory=database,
        config=config,
        now=NOW,
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 7),
    )

    assert result.decision == DashboardLinkDecision.SEND_LINK
    assert result.login_url is not None

    parsed = urlsplit(result.login_url)
    assert parsed.path == "/login"
    query = parse_qs(parsed.query)

    # Debe contener token, date_from y date_to
    assert "token" in query and len(query["token"][0]) > 0
    assert query["date_from"] == ["2026-09-01"]
    assert query["date_to"] == ["2026-09-07"]

    # No debe incluir next, tipo, categoria ni datos sensibles
    assert "next" not in query
    assert "tipo" not in query
    assert "categoria" not in query
    assert "user_id" not in query
    assert "usuario_id" not in query
    assert "whatsapp_id" not in query
    assert PHONE not in result.login_url


def test_generate_without_date_filters_preserves_clean_token_url(database, config):
    create_linked_user(database)
    result = DashboardLinkService.generate_or_reuse(
        PHONE,
        session_factory=database,
        config=config,
        now=NOW,
    )

    assert result.decision == DashboardLinkDecision.SEND_LINK
    parsed = urlsplit(result.login_url)
    query = parse_qs(parsed.query)
    assert set(query.keys()) == {"token"}


def test_generate_with_invalid_date_type_returns_error(database, config):
    create_linked_user(database)
    result = DashboardLinkService.generate_or_reuse(
        PHONE,
        session_factory=database,
        config=config,
        now=NOW,
        date_from="2026-09-01",  # string no permitido, debe ser date | None
    )
    assert result.decision == DashboardLinkDecision.ERROR
