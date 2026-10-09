import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.finance as finance_module
import app.services.wallet as wallet_module
from app.models.database import (
    Base,
    Billetera,
    MovimientoFinanciero,
    Usuario,
)
from app.services.finance import FinanceService
from app.services.wallet import WalletService


@pytest.fixture
def finance_db(monkeypatch):
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
    monkeypatch.setattr(finance_module, "SessionLocal", testing_session_local)
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


def test_register_whatsapp_text_auto_select_ars(finance_db):
    session = finance_db["session"]
    user = make_user(session)
    wallet = WalletService.create_wallet(user.id, "Efectivo", "ARS")

    res = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=user.whatsapp_id,
        whatsapp_message_id="msg-1",
        original_text="gaste 150.25 en cafe",
        llm_result={"movement_type": "egreso", "amount": "150.25", "description": "Cafe"},
    )
    assert res.status == "registered"
    assert res.wallet_id == wallet.wallet_id
    assert res.wallet_name == "Efectivo"
    assert res.currency == "ARS"
    assert res.amount == Decimal("150.25")
    mov = session.query(MovimientoFinanciero).filter(MovimientoFinanciero.id == uuid.UUID(res.movement_id)).first()
    assert mov is not None and str(mov.billetera_id) == wallet.wallet_id


def test_register_missing_and_ambiguous_and_foreign_rejects(finance_db):
    session = finance_db["session"]
    u1 = make_user(session, "U1", "5491111111111")
    u2 = make_user(session, "U2", "5492222222222")
    WalletService.create_wallet(u1.id, "W1", "ARS")
    w_foreign = WalletService.create_wallet(u2.id, "W2", "ARS")

    # missing USD wallet
    res_usd = FinanceService.register_movement_with_category(
        sender_phone=u1.whatsapp_id, whatsapp_message_id="m-usd", original_text="50 usd",
        movement_type="egreso", amount=Decimal("50.00"), currency="USD", description="Sub",
    )
    assert res_usd.status == "needs_wallet_creation"

    # foreign wallet
    res_for = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=u1.whatsapp_id, whatsapp_message_id="m-for", original_text="taxi",
        llm_result={"movement_type": "egreso", "amount": 100, "description": "Taxi"}, wallet_id=w_foreign.wallet_id,
    )
    assert res_for.status == "invalid_selection"

    # ambiguous
    WalletService.create_wallet(u1.id, "W1-bis", "ARS")
    res_amb = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=u1.whatsapp_id, whatsapp_message_id="m-amb", original_text="taxi",
        llm_result={"movement_type": "egreso", "amount": 100, "description": "Taxi"},
    )
    assert res_amb.status == "needs_wallet_selection"
    assert session.query(MovimientoFinanciero).count() == 0


def test_register_currency_mismatch_and_amount_validation(finance_db):
    session = finance_db["session"]
    user = make_user(session)
    w_usd = WalletService.create_wallet(user.id, "Dolares", "USD")

    res_mismatch = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=user.whatsapp_id, whatsapp_message_id="m-mis", original_text="gaste",
        llm_result={"movement_type": "egreso", "amount": 100, "currency": "ARS", "description": "Taxi"}, wallet_id=w_usd.wallet_id,
    )
    assert res_mismatch.status == "currency_mismatch"

    res_bad_amt = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=user.whatsapp_id, whatsapp_message_id="m-bad", original_text="gaste",
        llm_result={"movement_type": "egreso", "amount": "NaN", "description": "Taxi"},
    )
    assert res_bad_amt.status == "invalid_data"


def test_own_duplicate_and_foreign_duplicate_privacy(finance_db):
    session = finance_db["session"]
    u1 = make_user(session, "U1", "5491111111111")
    u2 = make_user(session, "U2", "5492222222222")
    w1 = WalletService.create_wallet(u1.id, "W1", "ARS")
    WalletService.create_wallet(u2.id, "W2", "ARS")

    res1 = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=u1.whatsapp_id, whatsapp_message_id="msg-shared", original_text="gaste 200",
        llm_result={"movement_type": "egreso", "amount": "200.00", "description": "Almuerzo"},
    )
    assert res1.status == "registered"

    # retry own duplicate
    res_dup = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=u1.whatsapp_id, whatsapp_message_id="msg-shared", original_text="gaste 200",
        llm_result={"movement_type": "egreso", "amount": "200.00", "description": "Almuerzo"},
    )
    assert res_dup.status == "duplicate" and res_dup.wallet_id == w1.wallet_id and res_dup.amount == Decimal("200.00")
    assert session.query(MovimientoFinanciero).count() == 1

    # foreign user same msg id
    res_foreign = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=u2.whatsapp_id, whatsapp_message_id="msg-shared", original_text="gaste 500",
        llm_result={"movement_type": "egreso", "amount": "500.00", "description": "Cena"},
    )
    assert res_foreign.status == "invalid_data" and res_foreign.movement_id is None


def test_update_movement_currency_wallet_and_reject_ambiguity_stale(finance_db):
    session = finance_db["session"]
    user = make_user(session)
    w_ars = WalletService.create_wallet(user.id, "Pesos", "ARS")
    w_usd = WalletService.create_wallet(user.id, "Dolares", "USD")

    res = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=user.whatsapp_id, whatsapp_message_id="msg-u", original_text="gaste 100",
        llm_result={"movement_type": "egreso", "amount": "100.00", "description": "Original"},
    )
    assert res.status == "registered"

    # stale expected wallet
    upd_stale = FinanceService.update_movement(
        sender_phone=user.whatsapp_id, movement_id=res.movement_id,
        changes={"description": "Nuevo"}, expected={"wallet_id": str(uuid.uuid4())},
    )
    assert upd_stale.status == "stale_context"

    # rejected change without partial mutation
    upd_bad = FinanceService.update_movement(
        sender_phone=user.whatsapp_id, movement_id=res.movement_id,
        changes={"currency": "EUR", "description": "Mutado"},
    )
    assert upd_bad.status in ("needs_wallet_creation", "invalid_data")
    mov = session.query(MovimientoFinanciero).filter(MovimientoFinanciero.id == uuid.UUID(res.movement_id)).first()
    assert mov.descripcion == "Original" and mov.moneda == "ARS"
    assert str(mov.billetera_id) == w_ars.wallet_id

    # Two USD wallets require explicit selection and preserve every shown field.
    WalletService.create_wallet(user.id, "Otros dolares", "USD")
    before = (mov.cantidad, mov.descripcion, mov.fecha_movimiento, mov.moneda, mov.billetera_id)
    ambiguous = FinanceService.update_movement(
        user.whatsapp_id, res.movement_id,
        {"currency": "USD", "amount": "25.50", "description": "Not saved", "fecha": "2026-10-04"},
    )
    assert ambiguous.status == "needs_wallet_selection"
    session.expire_all()
    assert (mov.cantidad, mov.descripcion, mov.fecha_movimiento, mov.moneda, mov.billetera_id) == before

    # valid update
    upd_ok = FinanceService.update_movement(
        sender_phone=user.whatsapp_id, movement_id=res.movement_id,
        changes={"currency": "USD", "wallet_id": w_usd.wallet_id},
    )
    assert upd_ok.status == "updated" and upd_ok.after.moneda == "USD" and upd_ok.after.wallet_id == w_usd.wallet_id
    assert upd_ok.after.cantidad == before[0]


@pytest.mark.parametrize("entry_type", ["text", "category"])
def test_register_entries_explicit_usd_absent_currency_success(finance_db, entry_type):
    session = finance_db["session"]
    user = make_user(session, name="USDUser", phone="5493333333333")
    wallet_usd = WalletService.create_wallet(user.id, "Dolares", "USD")
    assert wallet_usd.status == "created"
    assert session.query(Billetera).count() > 0

    if entry_type == "text":
        res = FinanceService.register_movement_from_whatsapp_text(
            sender_phone=user.whatsapp_id,
            whatsapp_message_id="msg-usd-explicit-text",
            original_text="gaste 20",
            llm_result={"movement_type": "egreso", "amount": "20.00", "currency": None, "description": "Hosting"},
            wallet_id=wallet_usd.wallet_id,
        )
    else:
        res = FinanceService.register_movement_with_category(
            sender_phone=user.whatsapp_id,
            whatsapp_message_id="msg-usd-explicit-cat",
            original_text="gaste 20",
            movement_type="egreso",
            amount=Decimal("20.00"),
            currency=None,
            description="Hosting",
            wallet_id=wallet_usd.wallet_id,
        )
    assert res.status == "registered"
    assert res.currency == "USD"
    assert res.amount == Decimal("20.00")
    assert res.wallet_id == wallet_usd.wallet_id


@pytest.mark.parametrize("bad_wallet", ["", "   "])
def test_register_entries_blank_wallet_id_rejected(finance_db, bad_wallet):
    session = finance_db["session"]
    user = make_user(session, name="BlankUser", phone="5494444444444")
    WalletService.create_wallet(user.id, "Pesos", "ARS")
    res = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=user.whatsapp_id,
        whatsapp_message_id="msg-blank-wallet",
        original_text="gaste 10",
        llm_result={"movement_type": "egreso", "amount": "10.00", "description": "Snack"},
        wallet_id=bad_wallet,
    )
    assert res.status == "invalid_selection"


@pytest.mark.parametrize("entry_type", ["text", "category"])
@pytest.mark.parametrize("failure", ["nonfinite", "commit"])
def test_both_entries_reject_nonfinite_and_never_confirm_failed_commit(finance_db, monkeypatch, entry_type, failure):
    session = finance_db["session"]
    user = make_user(session)
    WalletService.create_wallet(user.id, "Fixture ARS", "ARS")
    if failure == "commit":
        from sqlalchemy.orm import Session

        class FailedCommitSession(Session):
            def commit(self):
                raise RuntimeError("Synthetic commit failure")

        monkeypatch.setattr(finance_module, "SessionLocal", lambda: FailedCommitSession(bind=finance_db["engine"]))
    amount = "NaN" if failure == "nonfinite" else "20.00"
    if entry_type == "text":
        result = FinanceService.register_movement_from_whatsapp_text(
            user.whatsapp_id, "synthetic-failure", "synthetic movement",
            {"movement_type": "egreso", "amount": amount, "currency": "ARS", "description": "Fixture"},
        )
    else:
        result = FinanceService.register_movement_with_category(
            user.whatsapp_id, "synthetic-failure", "synthetic movement",
            "egreso", Decimal(amount), "ARS", "Fixture",
        )
    assert result.status == ("invalid_data" if failure == "nonfinite" else "persistence_error")
    assert result.movement_id is None
    assert session.query(MovimientoFinanciero).count() == 0
