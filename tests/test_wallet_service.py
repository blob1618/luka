import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.wallet as wallet_module
from app.models.database import (
    Base,
    Billetera,
    Categoria,
    MovimientoFinanciero,
    Usuario,
)
from app.services.wallet import WalletService


@pytest.fixture
def test_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(wallet_module, "SessionLocal", testing_session_local)

    session = testing_session_local()
    try:
        yield {"session": session, "engine": engine}
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def make_user(session, name="User1", phone="5491111111111"):
    u = Usuario(nombre=name, email=f"{uuid.uuid4()}@example.com", whatsapp_id=phone)
    session.add(u)
    session.commit()
    return u


@pytest.mark.parametrize("curr", ["ARS", "USD"])
def test_create_wallet_ars_and_usd(test_db, curr):
    session = test_db["session"]
    user = make_user(session)
    res = WalletService.create_wallet(user.id, f"Efectivo {curr}", curr)
    assert res.status == "created"
    assert res.wallet_id is not None
    assert res.currency == curr


@pytest.mark.parametrize("curr,expected", [("ARS", "Pesos"), ("USD", "Dólares")])
def test_create_wallet_empty_name_uses_currency_default(test_db, curr, expected):
    session = test_db["session"]
    user = make_user(session)
    res = WalletService.create_wallet(user.id, "", curr)
    assert res.status == "created"
    assert res.name == expected


def test_create_wallet_duplicate_currency_rejected(test_db):
    session = test_db["session"]
    user = make_user(session)
    res1 = WalletService.create_wallet(user.id, "Ahorros", "ARS")
    assert res1.status == "created"
    res2 = WalletService.create_wallet(user.id, "  ahorros  ", "ARS")
    assert res2.status == "wallet_already_exists"
    assert res2.wallet_id == res1.wallet_id
    assert res2.name == "Ahorros"
    assert session.query(Billetera).count() == 1


def test_create_wallet_same_name_different_currency(test_db):
    session = test_db["session"]
    user = make_user(session)
    res1 = WalletService.create_wallet(user.id, "Ahorros", "ARS")
    assert res1.status == "created"
    res2 = WalletService.create_wallet(user.id, "Ahorros", "USD")
    assert res2.status == "created"


def test_rename_wallet_own_and_foreign(test_db):
    session = test_db["session"]
    user = make_user(session)
    other = make_user(session, "Other", "5491100000099")
    created = WalletService.create_wallet(user.id, "Efectivo", "ARS")
    WalletService.create_wallet(other.id, "Ajena", "ARS")

    renamed = WalletService.rename_wallet(user.id, created.wallet_id, "  Principal  ")
    assert renamed.status == "renamed" and renamed.name == "Principal"
    assert WalletService.rename_wallet(other.id, created.wallet_id, "X").status == "wallet_not_found"
    assert WalletService.rename_wallet(user.id, created.wallet_id, "  ").status == "invalid_data"


def test_create_wallet_with_caller_session_does_not_commit(test_db):
    session = test_db["session"]
    user = make_user(session)

    result = WalletService.create_wallet(user.id, "Transactional", "ARS", session=session)

    assert result.status == "created"
    assert session.query(Billetera).filter(Billetera.nombre == "Transactional").count() == 1
    session.rollback()

    verify_session = sessionmaker(bind=test_db["engine"])()
    try:
        assert verify_session.query(Billetera).filter(Billetera.nombre == "Transactional").count() == 0
    finally:
        verify_session.close()


def test_create_wallet_integrity_error_preserves_caller_transaction(test_db):
    session = test_db["session"]
    user = make_user(session)
    session.add(Categoria(usuario_id=user.id, nombre="Pending category"))
    session.flush()

    @event.listens_for(session, "after_transaction_create")
    def inject_duplicate_after_savepoint_start(session, transaction):
        if transaction.nested:
            session.info["inject_duplicate_wallet"] = True

    @event.listens_for(session, "before_flush")
    def cause_concurrent_unique_conflict(session, _flush_context, _instances):
        if session.info.pop("inject_duplicate_wallet", False):
            session.add(Billetera(
                usuario_id=user.id,
                nombre="Concurrent",
                moneda="ARS",
            ))

    result = WalletService.create_wallet(user.id, "Concurrent", "ARS", session=session)

    assert result.status == "persistence_error"
    assert session.is_active
    assert session.query(Categoria).filter(Categoria.nombre == "Pending category").count() == 1
    session.commit()

    verify_session = sessionmaker(bind=test_db["engine"])()
    try:
        assert verify_session.query(Categoria).filter(Categoria.nombre == "Pending category").count() == 1
        assert verify_session.query(Billetera).count() == 0
    finally:
        verify_session.close()


def test_create_wallet_rejects_unsupported_currency(test_db):
    session = test_db["session"]
    user = make_user(session)

    result = WalletService.create_wallet(user.id, "Euros", "EUR")

    assert result.status == "invalid_data"
    assert session.query(Billetera).count() == 0


def test_wallet_privacy_and_list(test_db):
    session = test_db["session"]
    u1 = make_user(session, "U1", "5491111111111")
    u2 = make_user(session, "U2", "5492222222222")
    WalletService.create_wallet(u1.id, "W1", "ARS")
    WalletService.create_wallet(u2.id, "W2", "ARS")
    assert len(WalletService.list_wallets(u1.id)) == 1
    assert len(WalletService.list_wallets(u2.id)) == 1


def test_resolve_selection_auto_and_missing(test_db):
    session = test_db["session"]
    user = make_user(session)
    # 0 wallets -> missing
    assert WalletService.resolve_selection(session, user.id, currency="ARS").status == "needs_wallet_creation"
    # 1 wallet -> auto-select
    w1 = WalletService.create_wallet(user.id, "W1", "ARS")
    sel = WalletService.resolve_selection(session, user.id, currency="ARS")
    assert sel.status == "selected"
    assert sel.wallet_id == w1.wallet_id
    # Una segunda billetera de la misma moneda no se crea.
    assert WalletService.create_wallet(user.id, "W2", "ARS").status == "wallet_already_exists"
    assert WalletService.resolve_selection(session, user.id, currency="ARS").status == "selected"


def test_resolve_selection_explicit_valid_and_mismatch_and_foreign(test_db):
    session = test_db["session"]
    u1 = make_user(session, "U1", "5491111111111")
    u2 = make_user(session, "U2", "5492222222222")
    w_usd = WalletService.create_wallet(u1.id, "Viaje", "USD")
    w_foreign = WalletService.create_wallet(u2.id, "For", "USD")

    # inferred currency
    sel = WalletService.resolve_selection(session, u1.id, wallet_id=w_usd.wallet_id)
    assert sel.status == "selected" and sel.currency == "USD"
    # mismatch
    assert WalletService.resolve_selection(session, u1.id, currency="ARS", wallet_id=w_usd.wallet_id).status == "currency_mismatch"
    # foreign / nonexistent
    assert WalletService.resolve_selection(session, u1.id, wallet_id=w_foreign.wallet_id).status == "invalid_selection"
    assert WalletService.resolve_selection(session, u1.id, wallet_id=uuid.uuid4()).status == "invalid_selection"


def test_balances_income_expense_and_annulled_and_empty(test_db):
    session = test_db["session"]
    user = make_user(session)
    w = WalletService.create_wallet(user.id, "Billetera", "ARS")
    w_empty = WalletService.create_wallet(user.id, "Vacia", "USD")
    wid = uuid.UUID(w.wallet_id)

    session.add_all([
        MovimientoFinanciero(usuario_id=user.id, billetera_id=wid, tipo="ingreso", cantidad=Decimal("1000.50"), moneda="ARS", descripcion="Cobro"),
        MovimientoFinanciero(usuario_id=user.id, billetera_id=wid, tipo="egreso", cantidad=Decimal("300.25"), moneda="ARS", descripcion="Super"),
        MovimientoFinanciero(usuario_id=user.id, billetera_id=wid, tipo="egreso", cantidad=Decimal("500.00"), moneda="ARS", descripcion="Anulado", anulado_en=datetime.now(timezone.utc)),
    ])
    session.commit()

    b_map = {b.name: b for b in WalletService.get_balances_own_user(user.id, session=session)}
    assert b_map["Billetera"].balance == Decimal("700.25")
    assert b_map["Vacia"].balance == Decimal("0")
    assert b_map["Vacia"].wallet_id == w_empty.wallet_id


def test_balances_negative_more_than_five_and_arithmetic(test_db):
    session = test_db["session"]
    user = make_user(session)
    w_neg = WalletService.create_wallet(user.id, "Neg", "USD")
    WalletService.create_wallet(user.id, "Pesos", "ARS")
    session.add(MovimientoFinanciero(usuario_id=user.id, billetera_id=uuid.UUID(w_neg.wallet_id), tipo="egreso", cantidad=Decimal("150.00"), moneda="USD", descripcion="Gasto"))
    session.commit()
    balances = WalletService.get_balances_own_user(user.id, session=session)
    assert len(balances) == 2
    neg = next(b for b in balances if b.name == "Neg")
    assert neg.balance == Decimal("-150.00")

    # isolated large decimal arithmetic
    inc = Decimal("12345678901234567890.12345678901234567890")
    exp = Decimal("10000000000000000000.10000000000000000000")
    assert WalletService.calculate_balance_arithmetic(inc, exp) == Decimal("2345678901234567890.02345678901234567890")


def test_balances_multiwallet_usd_foreign_and_multiple_movements(test_db):
    session = test_db["session"]
    u1 = make_user(session, "MultiU1", "5498888888888")
    u2 = make_user(session, "MultiU2", "5499999999999")
    w1_ars = WalletService.create_wallet(u1.id, "Efectivo ARS", "ARS")
    w_usd = WalletService.create_wallet(u1.id, "Ahorro USD", "USD")
    w_foreign = WalletService.create_wallet(u2.id, "Foreign ARS", "ARS")
    assert session.query(Billetera).count() == 3

    wid1 = uuid.UUID(w1_ars.wallet_id)
    # 7 active movements in w1_ars
    for i in range(7):
        session.add(MovimientoFinanciero(
            usuario_id=u1.id, billetera_id=wid1, tipo="ingreso",
            cantidad=Decimal("100.125"), moneda="ARS", descripcion=f"Mov {i}",
        ))
    # 1 movement in USD
    session.add(MovimientoFinanciero(
        usuario_id=u1.id, billetera_id=uuid.UUID(w_usd.wallet_id), tipo="egreso",
        cantidad=Decimal("50.25"), moneda="USD", descripcion="Gasto USD",
    ))
    # Foreign movement
    session.add(MovimientoFinanciero(
        usuario_id=u2.id, billetera_id=uuid.UUID(w_foreign.wallet_id), tipo="ingreso",
        cantidad=Decimal("5000"), moneda="ARS", descripcion="Foreign mov",
    ))
    session.commit()

    balances = WalletService.get_balances_own_user(u1.id, session=session)
    assert len(balances) == 2
    b_map = {b.name: b for b in balances}
    assert b_map["Efectivo ARS"].income == Decimal("700.875")
    assert b_map["Efectivo ARS"].balance == Decimal("700.875")
    assert b_map["Ahorro USD"].balance == Decimal("-50.25")
