"""Modelo Billetera y vinculo nullable de movimientos (fase de expansion STK-237).

Estas pruebas cubren el contrato ORM y la integridad propietario/moneda sobre SQLite
aislado. El backfill SQL, la preservacion historica, RLS y los permisos de
`anon`/`authenticated` se verifican en el harness PostgreSQL descartable
(`supabase/tests/stk237_billetera.sql`), porque SQLite no implementa RLS.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import ForeignKeyConstraint, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import Base, Billetera, MovimientoFinanciero, Usuario


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


def create_user(session, *, whatsapp_id=None):
    user = Usuario(
        nombre="Usuario de prueba",
        email=f"{uuid.uuid4()}@example.com",
        whatsapp_id=whatsapp_id,
    )
    session.add(user)
    session.flush()
    return user


def create_wallet(session, user, *, moneda="ARS", nombre="Inicial"):
    wallet = Billetera(usuario_id=user.id, nombre=nombre, moneda=moneda)
    session.add(wallet)
    session.flush()
    return wallet


def create_movement(session, user, *, moneda="ARS", billetera=None, cantidad=Decimal("100.00"), tipo="egreso"):
    movement = MovimientoFinanciero(
        usuario_id=user.id,
        billetera_id=billetera.id if billetera is not None else None,
        tipo=tipo,
        cantidad=cantidad,
        moneda=moneda,
        fecha_movimiento=date(2026, 10, 1),
        origen="whatsapp_text",
    )
    session.add(movement)
    session.flush()
    return movement


def test_billetera_id_is_nullable_during_expansion():
    assert MovimientoFinanciero.__table__.c.billetera_id.nullable is True


def test_legacy_movement_without_wallet_is_accepted(session):
    user = create_user(session, whatsapp_id="5491100000001")
    create_wallet(session, user, moneda="ARS")

    movement = create_movement(session, user, moneda="ARS", billetera=None)
    session.commit()

    assert movement.billetera_id is None


def test_movement_links_to_wallet_of_same_owner_and_currency(session):
    user = create_user(session, whatsapp_id="5491100000002")
    wallet_ars = create_wallet(session, user, moneda="ARS")
    wallet_usd = create_wallet(session, user, moneda="USD")

    movement_ars = create_movement(session, user, moneda="ARS", billetera=wallet_ars)
    movement_usd = create_movement(session, user, moneda="USD", billetera=wallet_usd)
    session.commit()

    assert movement_ars.billetera_id == wallet_ars.id
    assert movement_usd.billetera_id == wallet_usd.id


def test_movement_can_be_linked_to_wallet_after_expansion(session):
    user = create_user(session, whatsapp_id="5491100000003")
    movement = create_movement(session, user, moneda="ARS", billetera=None)
    session.commit()

    wallet = create_wallet(session, user, moneda="ARS")
    movement.billetera_id = wallet.id
    session.commit()

    assert movement.billetera_id == wallet.id


def test_movement_rejects_wallet_of_another_user(session):
    owner = create_user(session, whatsapp_id="5491100000004")
    other = create_user(session, whatsapp_id="5491100000005")
    foreign_wallet = create_wallet(session, other, moneda="ARS")

    with pytest.raises(IntegrityError):
        create_movement(session, owner, moneda="ARS", billetera=foreign_wallet)
        session.commit()


def test_movement_rejects_wallet_of_another_currency(session):
    user = create_user(session, whatsapp_id="5491100000006")
    wallet_usd = create_wallet(session, user, moneda="USD")

    with pytest.raises(IntegrityError):
        create_movement(session, user, moneda="ARS", billetera=wallet_usd)
        session.commit()


@pytest.mark.parametrize("missing_field", ["usuario_id", "moneda", "nombre"])
def test_wallet_required_fields_reject_null(session, missing_field):
    user = create_user(session, whatsapp_id="5491100000007")
    values = {
        "id": uuid.uuid4(),
        "usuario_id": user.id,
        "nombre": "Inicial",
        "moneda": "ARS",
        "creado_en": datetime.now(timezone.utc),
    }
    values[missing_field] = None

    with pytest.raises(IntegrityError):
        session.execute(Billetera.__table__.insert().values(**values))
        session.commit()


@pytest.mark.parametrize("nombre", ["", "   "])
def test_wallet_name_cannot_be_blank(session, nombre):
    user = create_user(session, whatsapp_id="5491100000008")

    with pytest.raises(IntegrityError):
        create_wallet(session, user, nombre=nombre)
        session.commit()


def test_user_cannot_repeat_normalized_wallet_name_per_currency(session):
    user = create_user(session, whatsapp_id="5491100000009")
    create_wallet(session, user, moneda="ARS", nombre="Efectivo")

    with pytest.raises(IntegrityError):
        create_wallet(session, user, moneda="ARS", nombre="  efectivo  ")
        session.commit()


def test_same_wallet_name_is_allowed_in_another_currency(session):
    user = create_user(session, whatsapp_id="5491100000010")
    create_wallet(session, user, moneda="ARS", nombre="Efectivo")
    create_wallet(session, user, moneda="USD", nombre="Efectivo")
    session.commit()


def test_metadata_declares_wallet_integrity_objects():
    movements = MovimientoFinanciero.__table__
    wallet_fk = next(
        constraint
        for constraint in movements.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "movimientos_financieros_billetera_fkey"
    )

    assert [column.name for column in wallet_fk.columns] == [
        "billetera_id",
        "usuario_id",
        "moneda",
    ]
    assert [element.target_fullname for element in wallet_fk.elements] == [
        "billetera.id",
        "billetera.usuario_id",
        "billetera.moneda",
    ]
    assert "movimientos_financieros_billetera_id_idx" in {i.name for i in movements.indexes}

    wallet_table = Billetera.__table__
    assert "billetera_id_usuario_moneda_key" in {
        constraint.name for constraint in wallet_table.constraints
    }
    assert "billetera_nombre_no_vacio_check" in {
        constraint.name for constraint in wallet_table.constraints
    }
    assert {
        "billetera_usuario_moneda_nombre_uidx",
        "billetera_usuario_id_idx",
    } <= {index.name for index in wallet_table.indexes}
