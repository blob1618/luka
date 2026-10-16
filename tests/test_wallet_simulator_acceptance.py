"""End-to-end wallet flow through the same adapter used by the Streamlit simulator."""

import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import (
    Base,
    Billetera,
    Categoria,
    MovimientoFinanciero,
    Usuario,
)
from app.services.finance import FinanceService
from app.services.llm import LLMService
from app.services.wallet import WalletService
from testing.services.webhook_mode import WebhookModeService


@pytest.fixture()
def isolated_wallet_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    for target in (
        "app.services.dispatcher.SessionLocal",
        "app.services.finance.SessionLocal",
        "app.services.wallet.SessionLocal",
        "app.services.limit.SessionLocal",
        "app.services.category_creation.SessionLocal",
        "app.services.budget.SessionLocal",
        "app.services.onboarding.SessionLocal",
        "app.services.compensation.SessionLocal",
        "app.services.conversation_flow.SessionLocal",
        "app.services.reminder.SessionLocal",
        "app.services.chart_query.SessionLocal",
        "app.services.dashboard_link.SessionLocal",
        "app.scheduler.SessionLocal",
        "app.models.database.SessionLocal",
    ):
        monkeypatch.setattr(target, factory)

    yield factory
    engine.dispose()


def _balances_by_name(user_id):
    return {
        balance.name: balance
        for balance in WalletService.get_balances_own_user(user_id)
    }


@pytest.mark.asyncio
async def test_streamlit_simulator_wallet_flow_keeps_selection_currency_and_annulment_isolated(
    isolated_wallet_db, monkeypatch, fake_redis, capsys
):
    # Ensure WebhookModeService uses the isolated test FakeRedis client
    if not hasattr(fake_redis, "ttl"):
        async def _ttl(key: str) -> int:
            return fake_redis._expirations.get(str(key), 86400)

        fake_redis.ttl = _ttl

    if not hasattr(fake_redis, "aclose"):
        async def _aclose() -> None:
            pass

        fake_redis.aclose = _aclose

    monkeypatch.setattr(
        "testing.services.webhook_mode.redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    phone = "5491100000240"
    user_id = uuid.uuid4()
    foreign_phone = "5491100000241"
    foreign_user_id = uuid.uuid4()

    with isolated_wallet_db() as session:
        user = Usuario(
            id=user_id,
            nombre="Usuario STK240",
            email="stk240@example.test",
            whatsapp_id=phone,
        )
        foreign_user = Usuario(
            id=foreign_user_id,
            nombre="Usuario ajeno",
            email="stk240-foreign@example.test",
            whatsapp_id=foreign_phone,
        )
        session.add_all([user, foreign_user])
        session.add(Categoria(id=uuid.uuid4(), usuario_id=user_id, nombre="Comida"))
        session.commit()

    llm_responses = [
        {"intent": "create_wallet", "wallet_name": "Efectivo", "wallet_currency": "ARS"},
        {"intent": "create_wallet", "wallet_name": "Mercado Pago", "wallet_currency": "ARS"},
        {"intent": "create_wallet", "wallet_name": "Dólares", "wallet_currency": "USD"},
        {
            "intent": "expense",
            "movement_type": "egreso",
            "amount": "5400",
            "currency": "ARS",
            "description": "almuerzo",
            "category": "Comida",
        },
        {
            "intent": "expense",
            "movement_type": "ingreso",
            "amount": "375",
            "currency": "USD",
            "description": "trabajo freelance",
            "category": "Comida",
        },
        {"intent": "list_wallets"},
        {"intent": "delete_movement", "reference": "last_registered"},
        {"intent": "list_wallets"},
        {"intent": "list_wallets"},
    ]
    process_message = AsyncMock(side_effect=llm_responses)
    monkeypatch.setattr(LLMService, "process_message", process_message)

    simulator = WebhookModeService()

    async def send(text):
        return await simulator.send_message(
            text=text,
            phone=phone,
            provider="gemini",
            prompt_path="prompts/core_prompt.md",
        )

    created_ars = await send("Creá la billetera Efectivo en ARS")
    created_second_ars = await send("Creá la billetera Mercado Pago en ARS")
    created_usd = await send("Creá la billetera Dólares en USD")
    assert "Creé la billetera *Efectivo* en ARS" in created_ars.reply_text
    assert "Creé la billetera *Mercado Pago* en ARS" in created_second_ars.reply_text
    assert "Creé la billetera *Dólares* en USD" in created_usd.reply_text

    with isolated_wallet_db() as session:
        # In SQLite, func.now() has 1-second resolution so sub-second creations share
        # the same timestamp. Ensure distinct creado_en reflecting actual creation order
        # for deterministic ordering in list_wallets (order_by creado_en.asc()).
        wallets = {
            wallet.nombre: wallet
            for wallet in session.query(Billetera)
            .filter(Billetera.usuario_id == user_id)
            .all()
        }
        session.query(Billetera).filter(Billetera.id == wallets["Efectivo"].id).update(
            {"creado_en": datetime(2026, 10, 12, 10, 0, 0)}
        )
        session.query(Billetera).filter(Billetera.id == wallets["Mercado Pago"].id).update(
            {"creado_en": datetime(2026, 10, 12, 10, 0, 1)}
        )
        session.commit()
        wallet_ids = {name: w.id for name, w in wallets.items()}
    assert set(wallet_ids) == {"Efectivo", "Mercado Pago", "Dólares"}

    ambiguous = await send("Gasté 5400 en almuerzo")
    assert "más de una billetera en ARS" in ambiguous.reply_text
    assert "1. Efectivo" in ambiguous.reply_text
    assert "2. Mercado Pago" in ambiguous.reply_text
    assert ambiguous.redis_state is not None
    assert ambiguous.redis_state.get("step") == "awaiting_wallet_selection"
    with isolated_wallet_db() as session:
        assert (
            session.query(MovimientoFinanciero)
            .filter(MovimientoFinanciero.usuario_id == user_id)
            .count()
            == 0
        )

    selected = await send("2")
    assert "Registré tu egreso" in selected.reply_text
    assert selected.redis_state is not None
    assert selected.redis_state.get("step") == "none"
    assert selected.memory is not None
    with isolated_wallet_db() as session:
        ars_movement = (
            session.query(MovimientoFinanciero)
            .filter(MovimientoFinanciero.usuario_id == user_id)
            .one()
        )
        assert ars_movement.billetera_id == wallet_ids["Mercado Pago"]
        assert ars_movement.cantidad == Decimal("5400")
        assert ars_movement.moneda == "ARS"

    usd_registered = await send("Recibí 375 USD por trabajo freelance")
    assert "Registré tu ingreso" in usd_registered.reply_text

    before_annulment = await send("Mostrame mis billeteras")
    assert "Tus billeteras:" in before_annulment.reply_text
    assert "Efectivo:" in before_annulment.reply_text
    assert "Mercado Pago:" in before_annulment.reply_text
    assert "Dólares:" in before_annulment.reply_text
    assert "ARS" in before_annulment.reply_text
    assert "USD" in before_annulment.reply_text
    assert "Total" not in before_annulment.reply_text
    assert "999999" not in before_annulment.reply_text
    assert "Privada" not in before_annulment.reply_text

    balances_before = _balances_by_name(user_id)
    assert Decimal(str(balances_before["Efectivo"].balance)) == Decimal("0")
    assert Decimal(str(balances_before["Mercado Pago"].balance)) == Decimal("-5400")
    assert Decimal(str(balances_before["Dólares"].balance)) == Decimal("375")

    annulled = await send("Anulá el último movimiento")
    assert "Eliminé trabajo freelance" in annulled.reply_text
    with isolated_wallet_db() as session:
        usd_movement = (
            session.query(MovimientoFinanciero)
            .filter(
                MovimientoFinanciero.usuario_id == user_id,
                MovimientoFinanciero.moneda == "USD",
            )
            .one()
        )
        assert usd_movement.anulado_en is not None

    after_annulment = await send("Mostrame mis billeteras")
    assert "Tus billeteras:" in after_annulment.reply_text
    assert "Dólares:" in after_annulment.reply_text
    assert "USD" in after_annulment.reply_text
    assert "Total" not in after_annulment.reply_text

    balances_after = _balances_by_name(user_id)
    assert Decimal(str(balances_after["Efectivo"].balance)) == Decimal("0")
    assert Decimal(str(balances_after["Mercado Pago"].balance)) == Decimal("-5400")
    assert Decimal(str(balances_after["Dólares"].balance)) == Decimal("0")
    foreign_wallet = WalletService.create_wallet(
        user_id=foreign_user_id, name="Privada", currency="ARS"
    )
    foreign_movement = FinanceService.register_movement_from_whatsapp_text(
        sender_phone=foreign_phone,
        whatsapp_message_id="stk240-foreign-movement",
        original_text="ingreso privado",
        llm_result={
            "movement_type": "ingreso",
            "amount": "999999",
            "currency": "ARS",
            "description": "saldo privado",
        },
        wallet_id=foreign_wallet.wallet_id,
    )
    assert foreign_movement.status == "registered"

    isolated_reply = await send("Mostrame mis billeteras")
    assert "Tus billeteras:" in isolated_reply.reply_text
    assert "Efectivo:" in isolated_reply.reply_text
    assert "Dólares:" in isolated_reply.reply_text
    assert "Privada" not in isolated_reply.reply_text
    assert "999999" not in isolated_reply.reply_text
    assert process_message.await_count == 9

    captured = capsys.readouterr()
    assert "[CONVERSATION_FLOW] render fallback" not in captured.out
    assert "[CONVERSATION_FLOW] render fallback" not in captured.err
