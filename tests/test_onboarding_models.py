import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import (
    AcuerdoAceptado,
    AcuerdoVersion,
    Base,
    OnboardingInvitacion,
    Usuario,
)


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


def create_user(session, *, whatsapp_id=None, auth_user_id=None):
    user = Usuario(
        nombre="Usuario de prueba",
        email=f"{uuid.uuid4()}@example.com",
        whatsapp_id=whatsapp_id,
        auth_user_id=auth_user_id,
    )
    session.add(user)
    session.flush()
    return user


def create_invitation(session, *, whatsapp_id="5491100000000", **overrides):
    now = datetime.now(timezone.utc)
    values = {
        "whatsapp_id": whatsapp_id,
        "token_hash": f"hash-{uuid.uuid4()}",
        "estado": "pendiente",
        "expira_en": now + timedelta(hours=1),
        "creado_en": now,
    }
    values.update(overrides)
    invitation = OnboardingInvitacion(**values)
    session.add(invitation)
    return invitation


def test_multiple_users_can_have_null_auth_user_id(session):
    create_user(session, whatsapp_id="5491100000001")
    create_user(session, whatsapp_id="5491100000002")

    session.commit()


def test_auth_user_id_must_be_unique_when_present(session):
    auth_user_id = uuid.uuid4()
    create_user(session, auth_user_id=auth_user_id)

    with pytest.raises(IntegrityError):
        create_user(session, auth_user_id=auth_user_id)


def test_whatsapp_id_must_be_unique_when_present(session):
    create_user(session, whatsapp_id="5491100000003")

    with pytest.raises(IntegrityError):
        create_user(session, whatsapp_id="5491100000003")


@pytest.mark.parametrize("whatsapp_id", ["", "   "])
def test_empty_whatsapp_id_is_rejected(session, whatsapp_id):
    with pytest.raises(IntegrityError):
        create_user(session, whatsapp_id=whatsapp_id)


def test_invitation_token_hash_must_be_unique(session):
    token_hash = "hash-repetido"
    create_invitation(session, whatsapp_id="5491100000004", token_hash=token_hash)
    create_invitation(session, whatsapp_id="5491100000005", token_hash=token_hash)

    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token_hash", ""),
        ("token_hash", "   "),
        ("whatsapp_id", ""),
        ("whatsapp_id", "   "),
    ],
)
def test_invitation_identifiers_cannot_be_empty(session, field, value):
    create_invitation(session, **{field: value})

    with pytest.raises(IntegrityError):
        session.commit()


def test_only_one_pending_invitation_per_whatsapp(session):
    create_invitation(session, whatsapp_id="5491100000006")
    create_invitation(session, whatsapp_id="5491100000006")

    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("final_state", ["consumida", "revocada", "vencida"])
def test_new_pending_invitation_is_allowed_after_final_state(session, final_state):
    user = create_user(session, whatsapp_id=f"user-{final_state}")
    invitation = create_invitation(session, whatsapp_id=f"invite-{final_state}")
    session.commit()
    user_id = user.id

    with session.no_autoflush:
        invitation.estado = final_state
        if final_state == "consumida":
            invitation.consumida_en = datetime.now(timezone.utc)
            invitation.usuario_id = user_id
        elif final_state == "revocada":
            invitation.revocada_en = datetime.now(timezone.utc)
    session.commit()

    create_invitation(session, whatsapp_id=f"invite-{final_state}")
    session.commit()


@pytest.mark.parametrize("state", ["pendiente", "consumida", "revocada", "vencida"])
def test_invitation_state_matrix_accepts_valid_combinations(session, state):
    now = datetime.now(timezone.utc)
    values = {"estado": state}
    if state == "consumida":
        user = create_user(session)
        values.update(usuario_id=user.id, consumida_en=now)
    elif state == "revocada":
        values["revocada_en"] = now

    create_invitation(session, **values)
    session.commit()


@pytest.mark.parametrize(
    ("state", "with_user", "with_consumed_at", "with_revoked_at"),
    [
        ("pendiente", True, False, False),
        ("pendiente", False, True, False),
        ("pendiente", False, False, True),
        ("consumida", True, False, False),
        ("consumida", False, True, False),
        ("consumida", True, True, True),
        ("revocada", False, False, False),
        ("revocada", False, True, True),
        ("revocada", True, False, True),
        ("vencida", False, True, False),
        ("vencida", False, False, True),
        ("vencida", True, False, False),
    ],
)
def test_invitation_state_matrix_rejects_invalid_combinations(
    session, state, with_user, with_consumed_at, with_revoked_at
):
    now = datetime.now(timezone.utc)
    user = create_user(session) if with_user else None
    create_invitation(
        session,
        estado=state,
        usuario_id=user.id if user else None,
        consumida_en=now if with_consumed_at else None,
        revocada_en=now if with_revoked_at else None,
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_consumed_invitation_restricts_user_deletion(session):
    user = create_user(session)
    create_invitation(
        session,
        estado="consumida",
        usuario_id=user.id,
        consumida_en=datetime.now(timezone.utc),
    )
    session.commit()

    session.delete(user)
    with pytest.raises(IntegrityError):
        session.commit()


def test_invalid_invitation_state_is_rejected(session):
    create_invitation(session, estado="desconocida")

    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("field", ["intentos", "reenvios"])
def test_negative_invitation_counters_are_rejected(session, field):
    create_invitation(session, **{field: -1})

    with pytest.raises(IntegrityError):
        session.commit()


def test_invitation_expiration_must_be_after_creation(session):
    now = datetime.now(timezone.utc)
    create_invitation(session, creado_en=now, expira_en=now)

    with pytest.raises(IntegrityError):
        session.commit()


def test_agreement_version_must_be_unique(session):
    session.add_all(
        [
            AcuerdoVersion(version="1.0", contenido="Contenido A"),
            AcuerdoVersion(version="1.0", contenido="Contenido B"),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_only_one_agreement_version_can_be_current(session):
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            AcuerdoVersion(
                version="1.0",
                contenido="Contenido A",
                esta_vigente=True,
                vigente_desde=now,
            ),
            AcuerdoVersion(
                version="2.0",
                contenido="Contenido B",
                esta_vigente=True,
                vigente_desde=now,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_inactive_agreement_version_allows_null_effective_date(session):
    agreement = AcuerdoVersion(version="1.0", contenido="Contenido")
    session.add(agreement)
    session.commit()

    assert agreement.esta_vigente is False
    assert agreement.vigente_desde is None


def test_current_agreement_version_requires_effective_date(session):
    session.add(
        AcuerdoVersion(
            version="1.0",
            contenido="Contenido",
            esta_vigente=True,
            vigente_desde=None,
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_user_cannot_accept_same_agreement_version_twice(session):
    user = create_user(session)
    agreement = AcuerdoVersion(version="1.0", contenido="Contenido")
    session.add(agreement)
    session.flush()
    session.add_all(
        [
            AcuerdoAceptado(usuario_id=user.id, version_acuerdo_id=agreement.id),
            AcuerdoAceptado(usuario_id=user.id, version_acuerdo_id=agreement.id),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("null_field", ["usuario_id", "version_acuerdo_id", "aceptado_en"])
def test_required_acceptance_fields_reject_null(session, null_field):
    user = create_user(session)
    agreement = AcuerdoVersion(version="1.0", contenido="Contenido")
    session.add(agreement)
    session.flush()
    values = {
        "id": uuid.uuid4(),
        "usuario_id": user.id,
        "version_acuerdo_id": agreement.id,
        "aceptado_en": datetime.now(timezone.utc),
        "origen": "web_onboarding",
    }
    values[null_field] = None

    with pytest.raises(IntegrityError):
        session.execute(insert(AcuerdoAceptado).values(**values))


def test_acceptance_defaults_to_web_onboarding_origin(session):
    user = create_user(session)
    agreement = AcuerdoVersion(version="1.0", contenido="Contenido")
    session.add(agreement)
    session.flush()
    acceptance = AcuerdoAceptado(
        usuario_id=user.id,
        version_acuerdo_id=agreement.id,
    )
    session.add(acceptance)
    session.commit()

    assert acceptance.origen == "web_onboarding"


def test_acceptance_can_represent_unknown_legacy_origin(session):
    user = create_user(session)
    agreement = AcuerdoVersion(version="1.0", contenido="Contenido")
    session.add(agreement)
    session.flush()
    acceptance = AcuerdoAceptado(
        usuario_id=user.id,
        version_acuerdo_id=agreement.id,
        origen="legacy_desconocido",
    )
    session.add(acceptance)
    session.commit()

    assert acceptance.origen == "legacy_desconocido"


def test_rollback_does_not_disable_row_level_security():
    rollback_path = (
        Path(__file__).parents[1]
        / "database"
        / "migrations"
        / "003_onboarding_identity_consent.rollback.sql"
    )

    rollback_sql = rollback_path.read_text(encoding="utf-8").upper()

    assert "DISABLE ROW LEVEL SECURITY" not in rollback_sql


def test_migration_rejects_preexisting_auth_user_id_column():
    migration_path = (
        Path(__file__).parents[1]
        / "database"
        / "migrations"
        / "003_onboarding_identity_consent.sql"
    )

    migration_sql = migration_path.read_text(encoding="utf-8").upper()

    assert "ADD COLUMN IF NOT EXISTS AUTH_USER_ID" not in migration_sql
    assert "PG_CATALOG.PG_ATTRIBUTE" in migration_sql
    assert "PUBLIC.USUARIO.AUTH_USER_ID PREEXISTENTE" in migration_sql
    assert "ALTER TABLE PUBLIC.USUARIO\n  ADD COLUMN AUTH_USER_ID UUID NULL;" in migration_sql
