import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, OnboardingInvitacion, Usuario
from app.services.onboarding import (
    DEFAULT_INVITATION_TTL_MINUTES,
    DEFAULT_MAX_RESENDS,
    DEFAULT_REGISTRATION_URL,
    DEFAULT_RESEND_COOLDOWN_SECONDS,
    OnboardingConfig,
    OnboardingDecision,
    OnboardingService,
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
    return OnboardingConfig(
        registration_url="https://example.com/registro",
        invitation_ttl_minutes=30,
        resend_cooldown_seconds=60,
        max_resends=3,
    )


def prepare(database, config, *, now=NOW):
    return OnboardingService.prepare_whatsapp_message(
        PHONE,
        session_factory=database,
        config=config,
        now=now,
    )


def invitations(database):
    with database() as session:
        return session.query(OnboardingInvitacion).order_by(
            OnboardingInvitacion.creado_en
        ).all()


def test_known_user_allows_processing_without_invitation(database, config):
    with database() as session:
        session.add(
            Usuario(
                nombre="Usuario conocido",
                email="known@example.com",
                whatsapp_id=PHONE,
            )
        )
        session.commit()

    result = prepare(database, config)

    assert result.decision == OnboardingDecision.KNOWN_USER
    assert invitations(database) == []


def test_first_unknown_message_creates_pending_invitation(database, config):
    result = prepare(database, config)
    saved = invitations(database)

    assert result.decision == OnboardingDecision.SEND_INVITATION
    assert len(saved) == 1
    assert saved[0].estado == "pendiente"
    assert saved[0].reenvios == 0
    assert saved[0].ultimo_envio_en is not None
    assert saved[0].expira_en == (NOW + timedelta(minutes=30)).replace(tzinfo=None)


def test_raw_token_is_not_saved_and_hash_is_sha256(database, config, monkeypatch):
    raw_token = "token-original-super-secreto"
    monkeypatch.setattr("app.services.onboarding.secrets.token_urlsafe", lambda _: raw_token)

    result = prepare(database, config)
    saved = invitations(database)[0]

    assert raw_token in result.registration_url
    assert saved.token_hash != raw_token
    assert saved.token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def test_registration_link_contains_only_token_not_phone(database, config):
    result = prepare(database, config)
    parsed = urlsplit(result.registration_url)

    assert PHONE not in result.registration_url
    assert set(parse_qs(parsed.query)) == {"token"}
    assert parsed.path == "/registro"


def test_pending_invitation_inside_cooldown_is_suppressed(database, config):
    first_result = prepare(database, config)
    before = invitations(database)[0]
    before_id = before.id
    before_hash = before.token_hash

    second_result = prepare(database, config, now=NOW + timedelta(seconds=59))
    after = invitations(database)

    assert first_result.decision == OnboardingDecision.SEND_INVITATION
    assert second_result.decision == OnboardingDecision.SUPPRESS_RESPONSE
    assert len(after) == 1
    assert after[0].id == before_id
    assert after[0].token_hash == before_hash
    assert after[0].reenvios == 0


def test_resend_reuses_row_rotates_token_and_increments_counter(
    database,
    config,
    monkeypatch,
):
    tokens = iter(("first-token", "second-token"))
    monkeypatch.setattr(
        "app.services.onboarding.secrets.token_urlsafe",
        lambda _: next(tokens),
    )
    first_result = prepare(database, config)
    before = invitations(database)[0]
    invitation_id = before.id
    first_hash = before.token_hash

    second_result = prepare(database, config, now=NOW + timedelta(seconds=60))
    after = invitations(database)

    assert first_result.registration_url != second_result.registration_url
    assert second_result.decision == OnboardingDecision.SEND_INVITATION
    assert len(after) == 1
    assert after[0].id == invitation_id
    assert after[0].token_hash != first_hash
    assert after[0].token_hash == hashlib.sha256(b"second-token").hexdigest()
    assert after[0].reenvios == 1
    assert after[0].expira_en == (
        NOW + timedelta(seconds=60, minutes=30)
    ).replace(tzinfo=None)


def test_expired_invitation_is_closed_and_new_pending_is_created(database, config):
    old_created_at = NOW - timedelta(hours=1)
    old_invitation = OnboardingInvitacion(
        whatsapp_id=PHONE,
        token_hash=hashlib.sha256(b"expired-token").hexdigest(),
        estado="pendiente",
        expira_en=NOW - timedelta(seconds=1),
        reenvios=1,
        ultimo_envio_en=NOW - timedelta(minutes=31),
        creado_en=old_created_at,
        actualizado_en=old_created_at,
    )
    with database() as session:
        session.add(old_invitation)
        session.commit()
        old_id = old_invitation.id

    result = prepare(database, config)
    saved = invitations(database)

    assert result.decision == OnboardingDecision.SEND_INVITATION
    assert len(saved) == 2
    expired = next(row for row in saved if row.id == old_id)
    pending = next(row for row in saved if row.id != old_id)
    assert expired.estado == "vencida"
    assert pending.estado == "pendiente"
    assert pending.reenvios == 0


def test_maximum_resends_suppresses_new_response(database, config, monkeypatch):
    tokens = iter(f"token-{index}" for index in range(4))
    monkeypatch.setattr(
        "app.services.onboarding.secrets.token_urlsafe",
        lambda _: next(tokens),
    )
    prepare(database, config)
    for resend_number in range(1, 4):
        result = prepare(
            database,
            config,
            now=NOW + timedelta(seconds=60 * resend_number),
        )
        assert result.decision == OnboardingDecision.SEND_INVITATION

    before = invitations(database)[0]
    result = prepare(database, config, now=NOW + timedelta(seconds=240))
    after = invitations(database)

    assert result.decision == OnboardingDecision.SUPPRESS_RESPONSE
    assert len(after) == 1
    assert after[0].id == before.id
    assert after[0].token_hash == before.token_hash
    assert after[0].reenvios == 3


def test_zero_max_resends_allows_only_initial_send(database, config):
    zero_resends = OnboardingConfig(
        registration_url=config.registration_url,
        invitation_ttl_minutes=config.invitation_ttl_minutes,
        resend_cooldown_seconds=config.resend_cooldown_seconds,
        max_resends=0,
    )
    prepare(database, zero_resends)

    result = prepare(
        database,
        zero_resends,
        now=NOW + timedelta(seconds=60),
    )

    assert result.decision == OnboardingDecision.SUPPRESS_RESPONSE


def test_invalid_configuration_uses_safe_defaults(monkeypatch):
    monkeypatch.setenv("ONBOARDING_REGISTRATION_URL", "javascript:alert(1)")
    monkeypatch.setenv("ONBOARDING_INVITATION_TTL_MINUTES", "invalid")
    monkeypatch.setenv("ONBOARDING_RESEND_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("ONBOARDING_MAX_RESENDS", "-1")

    loaded = OnboardingConfig.from_env()

    assert loaded.registration_url == DEFAULT_REGISTRATION_URL
    assert loaded.invitation_ttl_minutes == DEFAULT_INVITATION_TTL_MINUTES
    assert loaded.resend_cooldown_seconds == DEFAULT_RESEND_COOLDOWN_SECONDS
    assert loaded.max_resends == DEFAULT_MAX_RESENDS


def test_database_error_returns_controlled_decision(config):
    class FailingQuery:
        def filter(self, *_args):
            raise RuntimeError("database unavailable")

    class FailingSession:
        def query(self, *_args):
            return FailingQuery()

        def rollback(self):
            pass

        def close(self):
            pass

    result = OnboardingService.prepare_whatsapp_message(
        PHONE,
        session_factory=lambda: FailingSession(),
        config=config,
        now=NOW,
    )

    assert result.decision == OnboardingDecision.ERROR


def test_session_creation_error_returns_controlled_decision(config):
    def unavailable_database():
        raise RuntimeError("database unavailable")

    result = OnboardingService.prepare_whatsapp_message(
        PHONE,
        session_factory=unavailable_database,
        config=config,
        now=NOW,
    )

    assert result.decision == OnboardingDecision.ERROR


def test_first_invitation_has_unique_identifier(database, config):
    result = prepare(database, config)
    saved = invitations(database)[0]

    assert result.decision == OnboardingDecision.SEND_INVITATION
    assert isinstance(saved.id, uuid.UUID)
