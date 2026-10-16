"""Focused hermetic unit tests for STK-239: wallets in conversational WhatsApp flow."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
from app.services.conversation import (
    ConversationService,
    PendingMovement,
)
from app.services.dispatcher import (
    _DispatchContext,
    _apply_pending_category_alternative,
    process_incoming_message,
)
from app.services.finance import FinanceService
from app.services.onboarding import OnboardingDecision, OnboardingResult


@pytest.fixture()
def isolated_db(monkeypatch):
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
        "app.models.database.SessionLocal",
    ):
        monkeypatch.setattr(target, factory)
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def no_pending_limit_flows(monkeypatch):
    for method in (
        "is_awaiting_limit_year_confirmation",
        "is_awaiting_limit_category_confirmation",
        "is_awaiting_limit_data",
        "is_awaiting_limit_delete_category",
        "is_awaiting_limit_month_selection",
        "is_awaiting_compensation_confirmation",
    ):
        monkeypatch.setattr(
            f"app.services.dispatcher.ConversationService.{method}",
            AsyncMock(return_value=False),
        )


def known_user():
    return OnboardingResult(OnboardingDecision.KNOWN_USER)


# ---------------------------------------------------------------------------
# Test 1: PendingMovement backward compatibility & string-only serialization
# ---------------------------------------------------------------------------

def test_pending_movement_backward_compatibility():
    # 1. New typed fields
    pm = PendingMovement(
        sender_phone="5491111111111",
        whatsapp_message_id="msg-1",
        original_text="gaste 100",
        movement_type="egreso",
        amount=Decimal("100.50"),
        currency="ARS",
        description="test",
        inferred_category="Comida",
        wallet_id="w-123",
        wallet_options=[{"ordinal": "1", "wallet_id": "w-1", "name": "Efectivo"}],
        resolved_fecha_movimiento=date(2026, 9, 1),
    )
    d = pm.to_dict()
    assert d["amount"] == "100.50"
    assert d["resolved_fecha_movimiento"] == "2026-09-01"
    assert d["wallet_options"] == [{"ordinal": "1", "wallet_id": "w-1", "name": "Efectivo"}]
    for opt in d["wallet_options"]:
        for val in opt.values():
            assert isinstance(val, str)

    # 2. Deserialization from new payload
    pm_restored = PendingMovement.from_dict(d)
    assert pm_restored.resolved_fecha_movimiento == date(2026, 9, 1)
    assert pm_restored.fecha_movimiento == date(2026, 9, 1)
    assert pm_restored.movement_date == date(2026, 9, 1)
    assert pm_restored.amount == Decimal("100.50")
    assert pm_restored.wallet_id == "w-123"

    # UUID wallet IDs are made JSON-safe on write
    pm.wallet_id = uuid.uuid4()
    assert pm.to_dict()["wallet_id"] == str(pm.wallet_id)

    # Legacy states can omit every newly added wallet/date field
    legacy_minimal = dict(d)
    for field_name in ("wallet_id", "wallet_options", "resolved_fecha_movimiento"):
        legacy_minimal.pop(field_name, None)
    pm_legacy_minimal = PendingMovement.from_dict(legacy_minimal)
    assert pm_legacy_minimal.wallet_id is None
    assert pm_legacy_minimal.wallet_options == []
    assert pm_legacy_minimal.resolved_fecha_movimiento is None

    # 3. Deserialization from legacy payload with 'movement_date'
    legacy_d1 = dict(d)
    del legacy_d1["resolved_fecha_movimiento"]
    legacy_d1["movement_date"] = "2026-08-15"
    pm_legacy1 = PendingMovement.from_dict(legacy_d1)
    assert pm_legacy1.resolved_fecha_movimiento == date(2026, 8, 15)

    # 4. Deserialization from legacy payload with 'fecha_movimiento'
    legacy_d2 = dict(d)
    del legacy_d2["resolved_fecha_movimiento"]
    legacy_d2["fecha_movimiento"] = "2026-07-20"
    pm_legacy2 = PendingMovement.from_dict(legacy_d2)
    assert pm_legacy2.resolved_fecha_movimiento == date(2026, 7, 20)


# ---------------------------------------------------------------------------
# Test 2: Ambiguity detection prompts wallet selection with 0 writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ambiguity_prompts_wallet_selection_zero_writes(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    cat_id = uuid.uuid4()
    sender_phone = "5491100000001"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS", creado_en=datetime(2026, 1, 1, 10, 0)))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS", creado_en=datetime(2026, 1, 1, 11, 0)))
        session.add(Categoria(id=cat_id, usuario_id=user_id, nombre="Transporte"))
        session.commit()

    llm_payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 5000,
        "currency": "ARS",
        "description": "nafta",
        "expense": "nafta",
        "category": "Transporte",
        "reply_text": "procesando",
        "fecha": "2026-10-09",
    }

    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        result = await process_incoming_message(sender_phone, "Gasté 5000 en nafta", whatsapp_message_id="msg-amb-1")

    assert "Tenés más de una billetera en ARS" in result.reply_text
    assert "1. Efectivo" in result.reply_text
    assert "2. Mercado Pago" in result.reply_text
    assert "Respondé con el número de la opción." in result.reply_text

    # Zero writes
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    # Redis state is awaiting_wallet_selection
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True
    pending = await ConversationService.get_pending_movement(sender_phone)
    assert pending is not None
    assert pending.amount == Decimal("5000")
    assert pending.currency == "ARS"
    assert pending.whatsapp_message_id == "msg-amb-1"
    assert pending.resolved_fecha_movimiento == date(2026, 10, 9)
    assert len(pending.wallet_options) == 2
    assert pending.wallet_options[0]["name"] == "Efectivo"
    assert pending.wallet_options[1]["name"] == "Mercado Pago"


# ---------------------------------------------------------------------------
# Test 2b: Ambiguity detection without category prompts wallet selection (0 writes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ambiguity_prompts_wallet_selection_zero_writes_without_category(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    sender_phone = "5491100000001b"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco1b@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS", creado_en=datetime(2026, 1, 1, 10, 0)))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS", creado_en=datetime(2026, 1, 1, 11, 0)))
        session.commit()

    llm_payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 3500,
        "currency": "ARS",
        "description": "kiosco",
        "expense": "kiosco",
        "category": None,
        "reply_text": "procesando",
        "fecha": "2026-10-10",
    }

    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        result = await process_incoming_message(sender_phone, "Gasté 3500 en kiosco", whatsapp_message_id="msg-amb-nocat")

    assert "Tenés más de una billetera en ARS" in result.reply_text
    assert "1. Efectivo" in result.reply_text
    assert "2. Mercado Pago" in result.reply_text

    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True
    pending = await ConversationService.get_pending_movement(sender_phone)
    assert pending is not None
    assert pending.amount == Decimal("3500")
    assert pending.inferred_category is None


# ---------------------------------------------------------------------------
# Test 2c: Deterministic needs_wallet_creation suppresses false success reply_text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_needs_wallet_creation_suppresses_model_false_success_with_and_without_category(isolated_db):
    user_id = uuid.uuid4()
    sender_phone = "5491100000099"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco99@example.com", whatsapp_id=sender_phone)
        session.add(user)
        # User has NO wallets at all
        session.commit()

    # Path A: With category and model reply_text falsely claiming registration success
    llm_payload_with_cat = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 10000,
        "currency": "ARS",
        "description": "supermercado",
        "expense": "supermercado",
        "category": "Comida",
        "reply_text": "¡Listo! Registré tu gasto de $10.000 en Comida exitosamente.",
    }

    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload_with_cat),
    ):
        result_with_cat = await process_incoming_message(sender_phone, "Gasté 10000 en el super", whatsapp_message_id="msg-nowallet-1")

    # Assert deterministic prompt and strict suppression of model's false success claim
    assert "No tenés ninguna billetera en ARS." in result_with_cat.reply_text
    assert "Creá una billetera primero para registrar movimientos en esa moneda." in result_with_cat.reply_text
    assert "exitosamente" not in result_with_cat.reply_text.lower()
    assert "¡listo!" not in result_with_cat.reply_text.lower()

    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    # Path B: Without category and model reply_text falsely claiming registration success
    llm_payload_no_cat = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 500,
        "currency": "USD",
        "description": "curso",
        "expense": "curso",
        "category": None,
        "reply_text": "Gasto guardado con éxito por 500 USD.",
    }

    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload_no_cat),
    ):
        result_no_cat = await process_incoming_message(sender_phone, "Pagué 500 USD curso", whatsapp_message_id="msg-nowallet-2")

    assert "No tenés ninguna billetera en USD." in result_no_cat.reply_text
    assert "éxito" not in result_no_cat.reply_text.lower()

    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0


# ---------------------------------------------------------------------------
# Test 3: Invalid selection rejects, preserves state, 0 writes, no leakage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_wallet_selection_reprompts_and_no_writes(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    sender_phone = "5491100000002"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco2@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS"))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS"))
        session.commit()

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id="msg-inv-1",
        original_text="5000 en nafta",
        movement_type="egreso",
        amount=Decimal("5000"),
        currency="ARS",
        description="nafta",
        inferred_category="Transporte",
        wallet_options=[
            {"ordinal": "1", "wallet_id": str(w1_id), "name": "Efectivo"},
            {"ordinal": "2", "wallet_id": str(w2_id), "name": "Mercado Pago"},
        ],
        resolved_fecha_movimiento=date(2026, 10, 9),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        result = await process_incoming_message(sender_phone, "5")

    assert "Opción no válida" in result.reply_text
    assert "1. Efectivo" in result.reply_text
    assert "2. Mercado Pago" in result.reply_text

    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True

    # Try foreign UUID
    foreign_uuid = str(uuid.uuid4())
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        result_foreign = await process_incoming_message(sender_phone, foreign_uuid)

    assert "Opción no válida" in result_foreign.reply_text
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True


# ---------------------------------------------------------------------------
# Test 4: Cancel wallet selection clears state with 0 writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_wallet_selection_clears_state(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    sender_phone = "5491100000003"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco3@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS"))
        session.commit()

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id="msg-cancel-1",
        original_text="5000 en nafta",
        movement_type="egreso",
        amount=Decimal("5000"),
        currency="ARS",
        description="nafta",
        inferred_category="Transporte",
        wallet_options=[{"ordinal": "1", "wallet_id": str(w1_id), "name": "Efectivo"}],
        resolved_fecha_movimiento=date(2026, 10, 9),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        result = await process_incoming_message(sender_phone, "cancelar")

    assert "cancelé" in result.reply_text.lower()
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False


# ---------------------------------------------------------------------------
# Test 4b: Reset context clears pending wallet selection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_context_clears_pending_wallet_selection(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    sender_phone = "5491100000003b"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco3b@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS"))
        session.commit()

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id="msg-reset-1",
        original_text="5000 en nafta",
        movement_type="egreso",
        amount=Decimal("5000"),
        currency="ARS",
        description="nafta",
        inferred_category="Transporte",
        wallet_options=[{"ordinal": "1", "wallet_id": str(w1_id), "name": "Efectivo"}],
        resolved_fecha_movimiento=date(2026, 10, 9),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        result = await process_incoming_message(sender_phone, "reiniciar")

    assert "arrancamos de cero" in result.reply_text.lower()
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False


# ---------------------------------------------------------------------------
# Test 5: Valid ordinal persists with exact wallet and confirms
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ambiguous_replies_keep_pending_until_valid_selection(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    cat_id = uuid.uuid4()
    sender_phone = "5491100000004"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco4@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS"))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS"))
        session.add(Categoria(id=cat_id, usuario_id=user_id, nombre="Transporte"))
        session.commit()

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id="msg-valid-1",
        original_text="5000 en nafta",
        movement_type="egreso",
        amount=Decimal("5000.75"),
        currency="ARS",
        description="nafta",
        inferred_category="Transporte",
        wallet_options=[
            {"ordinal": "1", "wallet_id": str(w1_id), "name": "Efectivo"},
            {"ordinal": "2", "wallet_id": str(w2_id), "name": "Mercado Pago"},
        ],
        resolved_fecha_movimiento=date(2026, 10, 8),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    for ambiguous_reply in ("gasté 2 pesos", "la primera o la segunda"):
        with patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=known_user(),
        ):
            ambiguous = await process_incoming_message(sender_phone, ambiguous_reply)

        assert "Opción no válida" in ambiguous.reply_text
        assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True
        with isolated_db() as session:
            assert session.query(MovimientoFinanciero).count() == 0

    # Only a reply that consists of an explicit option may persist the pending movement.

    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        result = await process_incoming_message(sender_phone, "2")

    assert "Registré tu egreso" in result.reply_text
    assert "$5000.75 ARS" in result.reply_text
    assert "Mercado Pago" in result.reply_text

    with isolated_db() as session:
        movs = session.query(MovimientoFinanciero).all()
        assert len(movs) == 1
        mov = movs[0]
        assert mov.usuario_id == user_id
        assert mov.billetera_id == w2_id
        assert mov.cantidad == Decimal("5000.75")
        assert mov.moneda == "ARS"
        assert mov.whatsapp_message_id == "msg-valid-1"
        assert mov.fecha_movimiento == date(2026, 10, 8)

    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False


@pytest.mark.asyncio
async def test_duplicate_selection_confirms_existing_movement_without_duplicate_write(isolated_db):
    user_id = uuid.uuid4()
    wallet_id = uuid.uuid4()
    category_id = uuid.uuid4()
    sender_phone = "5491100000010"
    message_id = "msg-duplicate-selection"

    with isolated_db() as session:
        session.add(Usuario(
            id=user_id,
            nombre="Franco",
            email="franco10@example.com",
            whatsapp_id=sender_phone,
        ))
        session.add(Billetera(
            id=wallet_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS"
        ))
        session.add(Categoria(id=category_id, usuario_id=user_id, nombre="Comida"))
        session.commit()

    original = FinanceService.register_movement_with_category(
        sender_phone=sender_phone,
        whatsapp_message_id=message_id,
        original_text="gaste 750 en cafe",
        movement_type="egreso",
        amount=Decimal("750"),
        currency="ARS",
        description="cafe",
        category_name="Comida",
        fecha_movimiento=date(2026, 10, 9),
        wallet_id=str(wallet_id),
    )
    assert original.status == "registered"

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id=message_id,
        original_text="gaste 750 en cafe",
        movement_type="egreso",
        amount=Decimal("750"),
        currency="ARS",
        description="cafe",
        inferred_category="Comida",
        wallet_options=[{
            "ordinal": "1", "wallet_id": str(wallet_id), "name": "Efectivo"
        }],
        resolved_fecha_movimiento=date(2026, 10, 9),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    with patch(
        "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
        return_value=known_user(),
    ):
        result = await process_incoming_message(sender_phone, "1")

    assert "ya había sido registrado" in result.reply_text
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 1


@pytest.mark.asyncio
async def test_category_alternative_preserves_pending_amount_and_message_context(monkeypatch):
    pending = PendingMovement(
        sender_phone="5491100000011",
        whatsapp_message_id="original-whatsapp-id",
        original_text="gaste 1250,75 en cafe",
        movement_type="egreso",
        amount=Decimal("1250.75"),
        currency="ARS",
        description="cafe",
        inferred_category="Otro",
        llm_result_extra={"amount": "1.00", "currency": "ARS", "fecha": "ayer"},
        wallet_id="wallet-original",
        resolved_fecha_movimiento=date(2026, 10, 8),
    )
    register = AsyncMock(return_value="registrado")
    monkeypatch.setattr("app.services.dispatcher._register_single_with_hint", register)
    monkeypatch.setattr(
        "app.services.dispatcher.ConversationService.clear_state", AsyncMock()
    )
    ctx = _DispatchContext(
        sender_phone=pending.sender_phone,
        text_body="cambiar a comidas",
        whatsapp_message_id="category-reply-id",
        conversation_history=None,
    )

    await _apply_pending_category_alternative(
        ctx, SimpleNamespace(pending_limit=None, pending_movement=pending), "Comidas"
    )

    args = register.await_args.args
    assert args[0:3] == (
        pending.sender_phone,
        pending.whatsapp_message_id,
        pending.original_text,
    )
    assert args[3]["amount"] is pending.amount
    assert args[3]["wallet_id"] == pending.wallet_id
    assert args[3]["fecha"] == "2026-10-08"


# ---------------------------------------------------------------------------
# Test 6: Wallet selection chains with category confirmation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wallet_selection_chains_category_confirmation(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    sender_phone = "5491100000005"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco5@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS"))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS"))
        session.commit()

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id="msg-cat-chain-1",
        original_text="5000 en parapente",
        movement_type="egreso",
        amount=Decimal("5000"),
        currency="ARS",
        description="parapente",
        inferred_category="Parapente",
        wallet_options=[
            {"ordinal": "1", "wallet_id": str(w1_id), "name": "Efectivo"},
            {"ordinal": "2", "wallet_id": str(w2_id), "name": "Mercado Pago"},
        ],
        resolved_fecha_movimiento=date(2026, 10, 7),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        result1 = await process_incoming_message(sender_phone, "1")

    assert "No tenés la categoría *Parapente*" in result1.reply_text
    assert "¿Querés crearla?" in result1.reply_text

    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert await ConversationService.is_awaiting_category_confirmation(sender_phone) is True
    pending_cat = await ConversationService.get_pending_movement(sender_phone)
    assert pending_cat.wallet_id == str(w1_id)
    assert pending_cat.resolved_fecha_movimiento == date(2026, 10, 7)

    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value={"intent": "confirm_category"}),
    ):
        result2 = await process_incoming_message(sender_phone, "sí")

    assert "Registré tu egreso" in result2.reply_text
    assert "parapente" in result2.reply_text.lower()
    assert result2.event_variables.get("category") == "Parapente"
    assert result2.event_variables.get("wallet_id") == str(w1_id)

    with isolated_db() as session:
        movs = session.query(MovimientoFinanciero).all()
        assert len(movs) == 1
        assert movs[0].billetera_id == w1_id
        assert movs[0].cantidad == Decimal("5000")
        assert movs[0].fecha_movimiento == date(2026, 10, 7)
        assert movs[0].whatsapp_message_id == "msg-cat-chain-1"
        assert movs[0].categoria_id is not None
        cat = session.query(Categoria).filter(Categoria.id == movs[0].categoria_id).first()
        assert cat is not None
        assert cat.nombre.lower() == "parapente"


@pytest.mark.asyncio
async def test_auto_selected_wallet_is_preserved_through_category_confirmation(isolated_db):
    user_id = uuid.uuid4()
    wallet_id = uuid.uuid4()
    sender_phone = "5491100000015"

    with isolated_db() as session:
        user = Usuario(
            id=user_id,
            nombre="Franco",
            email="franco15@example.com",
            whatsapp_id=sender_phone,
        )
        session.add(user)
        session.add(
            Billetera(
                id=wallet_id,
                usuario_id=user_id,
                nombre="Efectivo",
                moneda="ARS",
            )
        )
        session.commit()

    llm_payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 1250,
        "currency": "ARS",
        "description": "parapente",
        "expense": "parapente",
        "category": "Parapente",
        "reply_text": "",
        "fecha": "2026-10-07",
    }
    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=known_user(),
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=llm_payload,
        ),
    ):
        first = await process_incoming_message(
            sender_phone,
            "Gasté 1250 en parapente",
            whatsapp_message_id="msg-auto-wallet-category",
        )

    assert "¿Querés crearla?" in first.reply_text
    pending = await ConversationService.get_pending_movement(sender_phone)
    assert pending is not None
    assert pending.wallet_id == str(wallet_id)

    # A second same-currency wallet appearing mid-turn must not erase the
    # wallet that was auto-selected when the movement first arrived.
    with isolated_db() as session:
        session.add(
            Billetera(
                id=uuid.uuid4(),
                usuario_id=user_id,
                nombre="Mercado Pago",
                moneda="ARS",
            )
        )
        session.commit()

    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=known_user(),
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value={"intent": "confirm_category"},
        ),
    ):
        confirmed = await process_incoming_message(sender_phone, "sí")

    assert "Registré tu egreso" in confirmed.reply_text
    with isolated_db() as session:
        movements = session.query(MovimientoFinanciero).all()
        assert len(movements) == 1
        movement = movements[0]
        assert movement.billetera_id == wallet_id
        assert movement.whatsapp_message_id == "msg-auto-wallet-category"
        assert movement.cantidad == Decimal("1250")
        assert movement.fecha_movimiento == date(2026, 10, 7)


@pytest.mark.asyncio
async def test_deleted_wallet_during_category_confirmation_refreshes_selection(isolated_db):
    user_id = uuid.uuid4()
    old_wallet_id = uuid.uuid4()
    replacement_wallet_id = uuid.uuid4()
    sender_phone = "5491100000016"

    with isolated_db() as session:
        user = Usuario(
            id=user_id,
            nombre="Franco",
            email="franco16@example.com",
            whatsapp_id=sender_phone,
        )
        session.add(user)
        session.add(
            Billetera(
                id=old_wallet_id,
                usuario_id=user_id,
                nombre="Efectivo",
                moneda="ARS",
            )
        )
        session.commit()

    llm_payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 1750,
        "currency": "ARS",
        "description": "actividad",
        "expense": "actividad",
        "category": "Parapente",
        "reply_text": "",
        "fecha": "2026-10-06",
    }
    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=known_user(),
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=llm_payload,
        ),
    ):
        first = await process_incoming_message(
            sender_phone,
            "Gasté 1750 en una actividad",
            whatsapp_message_id="msg-deleted-wallet-category",
        )
    assert "¿Querés crearla?" in first.reply_text

    with isolated_db() as session:
        old_wallet = session.get(Billetera, old_wallet_id)
        session.delete(old_wallet)
        session.add(
            Billetera(
                id=replacement_wallet_id,
                usuario_id=user_id,
                nombre="Cuenta nueva",
                moneda="ARS",
            )
        )
        session.commit()

    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=known_user(),
        ),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value={"intent": "confirm_category"},
        ),
    ):
        refreshed = await process_incoming_message(sender_phone, "sí")

    assert "Solo hay una billetera disponible en ARS" in refreshed.reply_text
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0
    pending_selection = await ConversationService.get_pending_movement(sender_phone)
    assert pending_selection is not None
    assert pending_selection.wallet_options == [
        {
            "ordinal": "1",
            "wallet_id": str(replacement_wallet_id),
            "name": "Cuenta nueva",
        }
    ]

    with patch(
        "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
        return_value=known_user(),
    ):
        selected = await process_incoming_message(sender_phone, "1")
    assert "Registré tu egreso" in selected.reply_text
    assert "📁 Categoría: Parapente." in selected.reply_text
    assert await ConversationService.get_pending_movement(sender_phone) is None
    with isolated_db() as session:
        movements = session.query(MovimientoFinanciero).all()
        assert len(movements) == 1
        movement = movements[0]
        assert movement.billetera_id == replacement_wallet_id
        assert movement.whatsapp_message_id == "msg-deleted-wallet-category"
        assert movement.cantidad == Decimal("1750")
        assert movement.fecha_movimiento == date(2026, 10, 6)


# ---------------------------------------------------------------------------
# Test 7: Create wallet intent (success, duplicate, validation, user_not_found)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_wallet_intent(isolated_db):
    user_id = uuid.uuid4()
    sender_phone = "5491100000006"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco6@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.commit()

    llm_payload = {
        "intent": "create_wallet",
        "wallet_name": "Ualá",
        "wallet_currency": "ARS",
        "reply_text": "",
    }

    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        result = await process_incoming_message(sender_phone, "Creá la billetera Ualá en ARS")

    assert "Creé la billetera *Ualá* en ARS" in result.reply_text

    with isolated_db() as session:
        wallets = session.query(Billetera).filter(Billetera.usuario_id == user_id).all()
        assert len(wallets) == 1
        assert wallets[0].nombre == "Ualá"
        assert wallets[0].moneda == "ARS"

    # Try duplicate
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        result_dup = await process_incoming_message(sender_phone, "Creá la billetera Ualá en ARS")

    assert "Ya tenés una billetera llamada *Ualá* en ARS" in result_dup.reply_text

    # Missing name
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value={"intent": "create_wallet", "wallet_name": None}),
    ):
        result_missing = await process_incoming_message(sender_phone, "Creá una billetera")

    assert "¿Cómo querés llamar a la billetera?" in result_missing.reply_text

    # Default currency ARS when omitted
    default_currency_payload = {"intent": "create_wallet", "wallet_name": "Efectivo"}
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=default_currency_payload),
    ):
        result_default_currency = await process_incoming_message(
            sender_phone, "Creá una billetera Efectivo"
        )
    assert "Creé la billetera *Efectivo* en ARS" in result_default_currency.reply_text

    # Invalid currency
    invalid_currency_payload = {
        "intent": "create_wallet", "wallet_name": "Cripto", "wallet_currency": "EUR"
    }
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=invalid_currency_payload),
    ):
        result_invalid_currency = await process_incoming_message(
            sender_phone, "Creá una billetera Cripto en EUR"
        )
    assert "La moneda debe ser ARS o USD." in result_invalid_currency.reply_text
    with isolated_db() as session:
        wallets = session.query(Billetera).filter(Billetera.usuario_id == user_id).all()
        assert len(wallets) == 2

    # Unknown user
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        result_unlinked = await process_incoming_message("5499999999999", "Creá la billetera Ualá")

    assert "No encontré una cuenta vinculada a este WhatsApp." in result_unlinked.reply_text


# ---------------------------------------------------------------------------
# Test 8: List wallets with balances and user isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_wallets_intent_and_isolation(isolated_db):
    u1_id = uuid.uuid4()
    u2_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    w_foreign_id = uuid.uuid4()
    sender_phone = "5491100000007"
    other_phone = "5491100000008"

    with isolated_db() as session:
        session.add(Usuario(id=u1_id, nombre="U1", email="u1@example.com", whatsapp_id=sender_phone))
        session.add(Usuario(id=u2_id, nombre="U2", email="u2@example.com", whatsapp_id=other_phone))
        session.add(Billetera(id=w1_id, usuario_id=u1_id, nombre="Efectivo", moneda="ARS"))
        session.add(Billetera(id=w2_id, usuario_id=u1_id, nombre="Ahorro", moneda="USD"))
        session.add(Billetera(id=w_foreign_id, usuario_id=u2_id, nombre="Secreta", moneda="ARS"))

        session.add(MovimientoFinanciero(
            id=uuid.uuid4(), billetera_id=w1_id, usuario_id=u1_id, tipo="ingreso",
            cantidad=Decimal("15000"), moneda="ARS", descripcion="sueldo", fecha_movimiento=date(2026, 10, 1),
        ))
        session.add(MovimientoFinanciero(
            id=uuid.uuid4(), billetera_id=w1_id, usuario_id=u1_id, tipo="egreso",
            cantidad=Decimal("5000"), moneda="ARS", descripcion="super", fecha_movimiento=date(2026, 10, 2),
        ))
        session.add(MovimientoFinanciero(
            id=uuid.uuid4(), billetera_id=w2_id, usuario_id=u1_id, tipo="ingreso",
            cantidad=Decimal("500"), moneda="USD", descripcion="remesa", fecha_movimiento=date(2026, 10, 3),
        ))

        session.add(MovimientoFinanciero(
            id=uuid.uuid4(), billetera_id=w_foreign_id, usuario_id=u2_id, tipo="ingreso",
            cantidad=Decimal("999999"), moneda="ARS", descripcion="plata", fecha_movimiento=date(2026, 10, 1),
        ))
        session.commit()

    llm_payload = {"intent": "list_wallets", "reply_text": ""}

    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        result = await process_incoming_message(sender_phone, "Mostrame mis billeteras")

    assert "Tus billeteras:" in result.reply_text
    assert "Efectivo: $10000 ARS" in result.reply_text
    assert "Ahorro: $500 USD" in result.reply_text
    assert "Secreta" not in result.reply_text
    assert "999999" not in result.reply_text


# ---------------------------------------------------------------------------
# Test 9: Multi-turn wallet deleted/stale revalidation refreshes remaining choices
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiturn_wallet_deleted_after_prompt_refreshes_remaining_choices_and_persists(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    w3_id = uuid.uuid4()
    cat_id = uuid.uuid4()
    sender_phone = "5491100000021"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco21@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS", creado_en=datetime(2026, 1, 1, 10, 0)))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS", creado_en=datetime(2026, 1, 1, 11, 0)))
        session.add(Billetera(id=w3_id, usuario_id=user_id, nombre="Banco Galicia", moneda="ARS", creado_en=datetime(2026, 1, 1, 12, 0)))
        session.add(Categoria(id=cat_id, usuario_id=user_id, nombre="Supermercado"))
        session.commit()

    llm_payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 12000,
        "currency": "ARS",
        "description": "compras super",
        "expense": "compras super",
        "category": "Supermercado",
        "reply_text": "procesando",
        "fecha": "2026-10-10",
    }

    # Turn 1: Movement registration triggers ambiguity with 3 options
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        res1 = await process_incoming_message(sender_phone, "Gasté 12000 en el super", whatsapp_message_id="msg-del-1")

    assert "Tenés más de una billetera en ARS" in res1.reply_text
    assert "1. Efectivo" in res1.reply_text
    assert "2. Mercado Pago" in res1.reply_text
    assert "3. Banco Galicia" in res1.reply_text
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True

    # Between turns: W1 is deleted from the database
    with isolated_db() as session:
        w_to_del = session.query(Billetera).filter(Billetera.id == w1_id).first()
        session.delete(w_to_del)
        session.commit()

    # Turn 2: User responds selecting ordinal 1 (points to the now-deleted W1)
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        res2 = await process_incoming_message(sender_phone, "1", whatsapp_message_id="msg-del-2")

    # Assert zero movement writes, no success confirmation, refreshed current choices
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert "Esa opción ya no está disponible" in res2.reply_text
    assert "1. Mercado Pago" in res2.reply_text
    assert "2. Banco Galicia" in res2.reply_text
    assert "Efectivo" not in res2.reply_text
    assert "registré" not in res2.reply_text.lower()
    assert "éxito" not in res2.reply_text.lower()
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True

    pending2 = await ConversationService.get_pending_movement(sender_phone)
    assert pending2 is not None
    assert len(pending2.wallet_options) == 2
    assert pending2.wallet_options[0]["name"] == "Mercado Pago"
    assert pending2.wallet_options[1]["name"] == "Banco Galicia"

    # Turn 3: User responds with valid ordinal 1 (now pointing to Mercado Pago / W2)
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        res3 = await process_incoming_message(sender_phone, "1", whatsapp_message_id="msg-del-3")

    assert "Registré tu egreso" in res3.reply_text
    assert "Mercado Pago" in res3.reply_text
    assert "$12000" in res3.reply_text

    with isolated_db() as session:
        movs = session.query(MovimientoFinanciero).all()
        assert len(movs) == 1
        assert movs[0].billetera_id == w2_id
        assert movs[0].cantidad == Decimal("12000")
        assert movs[0].whatsapp_message_id == "msg-del-1"  # original message ID preserved

    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False


# ---------------------------------------------------------------------------
# Test 10: Multi-turn wallet deleted clears stale context when <= 1 choice remains
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiturn_wallet_deleted_after_prompt_clears_stale_context_when_insufficient_choices(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    sender_phone = "5491100000022"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco22@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS", creado_en=datetime(2026, 1, 1, 10, 0)))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS", creado_en=datetime(2026, 1, 1, 11, 0)))
        session.commit()

    llm_payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 4500,
        "currency": "ARS",
        "description": "farmacia",
        "expense": "farmacia",
        "category": None,
        "reply_text": "procesando",
        "fecha": "2026-10-10",
    }

    # Turn 1: Options presented (1. Efectivo, 2. Mercado Pago)
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        res1 = await process_incoming_message(sender_phone, "Gasté 4500 en farmacia", whatsapp_message_id="msg-stale-1")

    assert "Tenés más de una billetera en ARS" in res1.reply_text
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True

    # Between turns: W1 is deleted, leaving only 1 wallet (W2)
    with isolated_db() as session:
        w_to_del = session.query(Billetera).filter(Billetera.id == w1_id).first()
        session.delete(w_to_del)
        session.commit()

    # Turn 2: User responds selecting ordinal 1 (points to deleted W1)
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        res2 = await process_incoming_message(sender_phone, "1", whatsapp_message_id="msg-stale-2")

    # Assert zero movement writes, no success confirmation, stale context cleared
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert "La billetera elegida no está disponible. Por favor volvé a registrar el movimiento." in res2.reply_text
    assert "registré" not in res2.reply_text.lower()
    assert "éxito" not in res2.reply_text.lower()
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False
    assert await ConversationService.get_pending_movement(sender_phone) is None


# ---------------------------------------------------------------------------
# Test 11: Multi-turn wrong-currency choice after options stored revalidates and refreshes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiturn_wallet_currency_changed_after_prompt_refreshes_choices_and_persists(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    w3_id = uuid.uuid4()
    sender_phone = "5491100000023"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco23@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS", creado_en=datetime(2026, 1, 1, 10, 0)))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS", creado_en=datetime(2026, 1, 1, 11, 0)))
        session.add(Billetera(id=w3_id, usuario_id=user_id, nombre="Banco", moneda="ARS", creado_en=datetime(2026, 1, 1, 12, 0)))
        session.commit()

    llm_payload = {
        "intent": "expense",
        "movement_type": "egreso",
        "amount": 2500,
        "currency": "ARS",
        "description": "librería",
        "expense": "librería",
        "category": None,
        "reply_text": "procesando",
        "fecha": "2026-10-10",
    }

    # Turn 1: 3 ARS options shown
    with (
        patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()),
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock, return_value=llm_payload),
    ):
        res1 = await process_incoming_message(sender_phone, "Gasté 2500 en librería", whatsapp_message_id="msg-curr-1")

    assert "Tenés más de una billetera en ARS" in res1.reply_text
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True

    # Between turns: W1's currency changed from ARS to USD
    with isolated_db() as session:
        w1 = session.query(Billetera).filter(Billetera.id == w1_id).first()
        w1.moneda = "USD"
        session.commit()

    # Turn 2: User responds selecting ordinal 1 (points to W1, now USD)
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        res2 = await process_incoming_message(sender_phone, "1", whatsapp_message_id="msg-curr-2")

    # Assert zero movement writes, currency mismatch detected, options refreshed to remaining ARS wallets
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert "Esa opción ya no está disponible" in res2.reply_text
    assert "1. Mercado Pago" in res2.reply_text
    assert "2. Banco" in res2.reply_text
    assert "Efectivo" not in res2.reply_text
    assert "registré" not in res2.reply_text.lower()
    assert "éxito" not in res2.reply_text.lower()
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is True

    # Turn 3: User selects ordinal 2 (Banco / W3)
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        res3 = await process_incoming_message(sender_phone, "2", whatsapp_message_id="msg-curr-3")

    assert "Registré tu egreso" in res3.reply_text
    assert "Banco" in res3.reply_text

    with isolated_db() as session:
        movs = session.query(MovimientoFinanciero).all()
        assert len(movs) == 1
        assert movs[0].billetera_id == w3_id
        assert movs[0].moneda == "ARS"

    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False


# ---------------------------------------------------------------------------
# Test 12: Multi-turn wrong-currency or foreign ownership clears stale context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiturn_wallet_currency_changed_clears_context_when_insufficient_choices(isolated_db):
    user_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    sender_phone = "5491100000024"

    with isolated_db() as session:
        user = Usuario(id=user_id, nombre="Franco", email="franco24@example.com", whatsapp_id=sender_phone)
        session.add(user)
        session.add(Billetera(id=w1_id, usuario_id=user_id, nombre="Efectivo", moneda="ARS"))
        session.add(Billetera(id=w2_id, usuario_id=user_id, nombre="Mercado Pago", moneda="ARS"))
        session.commit()

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id="msg-curr-stale-1",
        original_text="1500 taxi",
        movement_type="egreso",
        amount=Decimal("1500"),
        currency="ARS",
        description="taxi",
        inferred_category=None,
        wallet_options=[
            {"ordinal": "1", "wallet_id": str(w1_id), "name": "Efectivo"},
            {"ordinal": "2", "wallet_id": str(w2_id), "name": "Mercado Pago"},
        ],
        resolved_fecha_movimiento=date(2026, 10, 10),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    # Between turns: W1 changed to USD (leaving only 1 ARS wallet: W2)
    with isolated_db() as session:
        w1 = session.query(Billetera).filter(Billetera.id == w1_id).first()
        w1.moneda = "USD"
        session.commit()

    # User picks option 1 (now USD)
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        res = await process_incoming_message(sender_phone, "1", whatsapp_message_id="msg-curr-stale-2")

    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert "La billetera elegida no está disponible. Por favor volvé a registrar el movimiento." in res.reply_text
    assert "registré" not in res.reply_text.lower()
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False
    assert await ConversationService.get_pending_movement(sender_phone) is None


@pytest.mark.asyncio
async def test_multiturn_wallet_ownership_changed_prevents_write_and_clears_context(isolated_db):
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()
    w1_id = uuid.uuid4()
    w2_id = uuid.uuid4()
    sender_phone = "5491100000025"
    other_phone = "5491100000026"

    with isolated_db() as session:
        session.add(Usuario(id=user_a_id, nombre="User A", email="usera@example.com", whatsapp_id=sender_phone))
        session.add(Usuario(id=user_b_id, nombre="User B", email="userb@example.com", whatsapp_id=other_phone))
        session.add(Billetera(id=w1_id, usuario_id=user_a_id, nombre="Efectivo A", moneda="ARS"))
        session.add(Billetera(id=w2_id, usuario_id=user_a_id, nombre="Digital A", moneda="ARS"))
        session.commit()

    pending = PendingMovement(
        sender_phone=sender_phone,
        whatsapp_message_id="msg-owner-1",
        original_text="1000 kiosco",
        movement_type="egreso",
        amount=Decimal("1000"),
        currency="ARS",
        description="kiosco",
        inferred_category=None,
        wallet_options=[
            {"ordinal": "1", "wallet_id": str(w1_id), "name": "Efectivo A"},
            {"ordinal": "2", "wallet_id": str(w2_id), "name": "Digital A"},
        ],
        resolved_fecha_movimiento=date(2026, 10, 10),
    )
    await ConversationService.set_pending_wallet_selection(sender_phone, pending)

    # Between turns: W1 transferred to User B
    with isolated_db() as session:
        w1 = session.query(Billetera).filter(Billetera.id == w1_id).first()
        w1.usuario_id = user_b_id
        session.commit()

    # User A responds with option 1
    with patch("app.services.dispatcher.OnboardingService.prepare_whatsapp_message", return_value=known_user()):
        res = await process_incoming_message(sender_phone, "1", whatsapp_message_id="msg-owner-2")

    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0

    assert "La billetera elegida no está disponible. Por favor volvé a registrar el movimiento." in res.reply_text
    assert "User B" not in res.reply_text
    assert "registré" not in res.reply_text.lower()
    assert await ConversationService.is_awaiting_wallet_selection(sender_phone) is False
