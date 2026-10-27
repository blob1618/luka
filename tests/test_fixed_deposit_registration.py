"""Synthetic STK-254 regressions; no claim about the original QA deployment."""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.models.database import Billetera, Categoria, MovimientoFinanciero, Usuario
from app.services.conversation import ConversationService, PendingMovement
from app.services.dispatcher import _resume_movement_after_category, process_incoming_message
from app.services.finance import FinanceService
from app.services.llm import LLMService
from app.services.movement_dates import argentina_today
from app.services.onboarding import OnboardingDecision, OnboardingResult
from app.services.wallet import WalletService


PLACEMENT = "Listo, ya metí 50 lucas de ese sobrante en un Plazo Fijo, anotámelo"
LATER_REINVESTMENT = "Cobré 500 de intereses del plazo fijo que constituí ayer y después reinvertí 50000"
COMMA_REINVESTMENT = "Cobré 500 de intereses del plazo fijo que constituí ayer, después reinvertí 50000"
REINVESTMENT_FIRST = "Reinvertí 50000 y cobré 500 de intereses del plazo fijo"
UNCERTAIN_TEMPORAL_CASES = {
    "Cobré 500 de intereses del plazo fijo que constituí ayer",
    "Cobré 500 de intereses del plazo fijo que constituí ayer y después renové mi seguro",
    "Cobré 500 de intereses del plazo fijo que constituí ayer, después renové mi seguro",
    "Cobré 500 de intereses del plazo fijo que constituí ayer. Después renové mi seguro",
    "Cobré 500 de intereses del plazo fijo que constituí ayer. Reinvertí 50000 en mi negocio",
    "Reinvertí 50000 en mi negocio. Cobré 500 de intereses del plazo fijo que constituí ayer",
    "Cobré 500 de intereses del plazo fijo que el mes pasado constituí",
}


@pytest.fixture
def account(isolated_db, monkeypatch):
    with isolated_db() as session:
        user = Usuario(
            nombre="Synthetic", email="stk254@example.test", whatsapp_id="stk254-user",
            proactivo_habilitado=False,
        )
        session.add(user)
        session.flush()
        wallet = Billetera(usuario_id=user.id, nombre="Cuenta", moneda="ARS")
        category = Categoria(usuario_id=user.id, nombre="Transporte")
        session.add_all([wallet, category])
        session.flush()
        session.add_all([
            MovimientoFinanciero(
                usuario_id=user.id, billetera_id=wallet.id, tipo=kind,
                cantidad=amount, moneda="ARS", descripcion="baseline",
                fecha_movimiento=date(2026, 9, 10),
            )
            for kind, amount in (("ingreso", 100000), ("egreso", 5000))
        ])
        session.commit()
        identifiers = (user.id, str(wallet.id))
    monkeypatch.setattr(
        "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
        lambda _: OnboardingResult(OnboardingDecision.KNOWN_USER),
    )
    monkeypatch.setattr(
        "app.services.dispatcher.ConversationFlowRuntime.render_event",
        AsyncMock(return_value=None),
    )
    return isolated_db, *identifiers


def snapshot(account):
    factory, user_id, _ = account
    with factory() as session:
        rows = session.query(MovimientoFinanciero).count()
    return rows, WalletService.get_balances(user_id=user_id)


def assert_rejected(reply, event_key=None):
    assert "plazo fijo" in reply.lower()
    assert "separad" in reply.lower()
    assert "✅" not in reply
    assert event_key is None


def register(method, text, movement_type="ingreso", amount=50000):
    if method == "parsed":
        return FinanceService.register_movement_from_whatsapp_text(
            sender_phone="stk254-user", whatsapp_message_id="stk254-request",
            original_text=text,
            llm_result={
                "intent": "expense", "movement_type": movement_type, "amount": amount,
                "currency": "ARS", "description": "sueldo", "category": None,
            },
        )
    return FinanceService.register_movement_with_category(
        sender_phone="stk254-user", whatsapp_message_id="stk254-request",
        original_text=text, movement_type=movement_type, amount=Decimal(amount),
        currency="ARS", description="sueldo",
    )


@pytest.mark.parametrize("method", ["parsed", "category"])
@pytest.mark.parametrize("movement_type", ["ingreso", "egreso", None])
def test_principal_is_rejected_before_financial_validation(account, method, movement_type):
    before = snapshot(account)
    result = register(method, PLACEMENT, movement_type)
    assert result.status == "unsupported_operation"
    assert_rejected(result.message)
    assert snapshot(account) == before


@pytest.mark.parametrize("method", ["parsed", "category"])
@pytest.mark.parametrize("movement_type", ["ingreso", "egreso", None])
@pytest.mark.parametrize("text", [LATER_REINVESTMENT, COMMA_REINVESTMENT, REINVESTMENT_FIRST])
def test_coordinated_reinvestment_cannot_be_registered_as_cash_flow(account, method, movement_type, text):
    before = snapshot(account)
    result = register(method, text, movement_type)
    assert result.status == "unsupported_operation"
    assert_rejected(result.message)
    assert snapshot(account) == before


@pytest.mark.parametrize("method", ["parsed", "category"])
@pytest.mark.parametrize("movement_type", ["ingreso", "egreso"])
@pytest.mark.parametrize("text", [
    "Constituí un plazo fijo con 50000 de mis ahorros",
    "Renové el plazo fijo de 50000",
    "Reinvertí 50000 de capital e intereses en un plazo fijo",
    "Coloqué mis ahorros en un plazo fijo",  # Missing amount cannot bypass the guard.
    "Anotá 50000 del plazo fijo",  # No distinction between principal and earnings.
    "Cobré capital e intereses del plazo fijo, anotá 51000",
    "Anotá la renovación de mi plazo fijo por 51000, incluyendo intereses",
    "Registrá la constitución de un plazo fijo con intereses por 51000",
    "Registrá una colocación de 50000 en un plazo fijo con intereses al vencimiento",
    "Pagué 500 de comisión. Registrá una colocación de 50000 en un plazo fijo con intereses al vencimiento",
    "Anotá el plazo fijo que constituí ayer por 50000, con intereses al vencimiento",
    "Cobré 500 de intereses del plazo fijo y anotá una colocación de 50000 con intereses al vencimiento",
    "Cobré 500 de sueldo, anotá 50000 del plazo fijo que constituí ayer con intereses al vencimiento",
    "Anotá 50000 del plazo fijo que constituí ayer con los intereses que cobre al vencimiento",
    "Anotá 500 de intereses que cobre al vencimiento del plazo fijo",
    "Pagué 500 por el plazo fijo",
    "Gasté 500 en el plazo fijo",
    "Gasté 500 por el plazo fijo",
    "Gasté 500 en el plazo fijo por su renovación",
    "Pagué 500 en un aporte para el plazo fijo",
    "Cobré 500 de intereses del plazo fijo y reinvertí 50000",
    "Cobré 500 de intereses del plazo fijo y reinvertí 50000 con intereses al vencimiento",
    "Cobré 500 de intereses del plazo fijo y reinvertí todo",
])
def test_unsupported_and_ambiguous_principal_leave_totals_unchanged(account, method, movement_type, text):
    before = snapshot(account)
    assert register(method, text, movement_type).status == "unsupported_operation"
    assert snapshot(account) == before


@pytest.mark.parametrize("method", ["parsed", "category"])
@pytest.mark.parametrize(("text", "kind"), [
    ("Me depositaron 50000 de sueldo", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Me acreditaron 500 de rendimiento del plazo fijo", "ingreso"),
    ("Pagué 500 de comisión por el plazo fijo", "egreso"),
    ("Pagué 500 de honorarios al contador por el plazo fijo", "egreso"),
    ("Pagué 500 de honorario al contador por el plazo fijo", "egreso"),
    ("Gasté 500 en taxi para ir al banco por el plazo fijo", "egreso"),
    ("Gasté 500 en taxi por el plazo fijo", "egreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí el mes pasado", "ingreso"),
    ("Pagué 500 de comisión por el plazo fijo constituido el mes pasado", "egreso"),
    ("Pagué 500 de comisión por la renovación del plazo fijo", "egreso"),
    ("Cobré 500 de intereses del plazo fijo que había constituido", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer", "ingreso"),
    ("Pagué 500 de comisión por el plazo fijo que renové ayer", "egreso"),
    ("Cobré 500 de intereses de mi depósito a plazo fijo", "ingreso"),
    ("Cobré 500 de intereses de la colocación a plazo fijo", "ingreso"),
    ("Por la colocación a plazo fijo pagué 500 de comisión", "egreso"),
    ("Cobré 500 de intereses del plazo fijo que el mes pasado constituí", "ingreso"),
    ("Anotá 500 de intereses que cobré del plazo fijo", "ingreso"),
    ("Cobré 500 de interés del plazo fijo", "ingreso"),
    ("Renové mi seguro. Cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo y renové mi seguro", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer y después renové mi seguro", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer, después renové mi seguro", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer. Después renové mi seguro", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer. Reinvertí 50000 en mi negocio", "ingreso"),
    ("Reinvertí 50000 en mi negocio. Cobré 500 de intereses del plazo fijo que constituí ayer", "ingreso"),
    ("Reinvertí 50000 en mi negocio y cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Renové mi seguro y cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Reinvertí 50000. Cobré 500 de intereses del plazo fijo", "ingreso"),
])
def test_real_income_and_expenses_remain_registrable(account, method, text, kind):
    result = register(method, text, kind, amount=500)
    assert result.status == "registered"
    rows, wallets = snapshot(account)
    assert rows == 3
    assert wallets[0].income == (Decimal(100500) if kind == "ingreso" else Decimal(100000))
    assert wallets[0].expense == (Decimal(5500) if kind == "egreso" else Decimal(5000))


@pytest.mark.asyncio
@pytest.mark.parametrize("movement_type", ["ingreso", "egreso", None])
@pytest.mark.parametrize("text", [
    PLACEMENT,
    LATER_REINVESTMENT,
    COMMA_REINVESTMENT,
    REINVESTMENT_FIRST,
    "Anotá el plazo fijo que constituí ayer por 50000, con intereses al vencimiento",
    "Registrá una colocación de 50000 en un plazo fijo con intereses al vencimiento",
    "Cobré 500 de sueldo, anotá 50000 del plazo fijo que constituí ayer con intereses al vencimiento",
    "Anotá 50000 del plazo fijo que constituí ayer con los intereses que cobre al vencimiento",
    "Pagué 500 por el plazo fijo",
    "Gasté 500 en el plazo fijo",
    "Gasté 500 por el plazo fijo",
    "Gasté 500 en el plazo fijo por su renovación",
    "Cobré 500 de intereses del plazo fijo y reinvertí 50000",
])
async def test_provider_misclassification_cannot_confirm_or_write(account, monkeypatch, movement_type, text):
    before = snapshot(account)
    provider = AsyncMock()
    provider.generate_json.return_value = {
        "intent": "expense", "movement_type": movement_type, "amount": 50000,
        "description": "sueldo", "reply_text": "✅ Ya quedó registrado.",
    }
    monkeypatch.setattr(LLMService, "_get_provider", lambda: provider)
    result = await process_incoming_message("stk254-user", text, "stk254-request")
    provider.generate_json.assert_awaited_once()
    assert_rejected(result.reply_text, result.event_key)
    assert result.reply_message is None
    assert snapshot(account) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "kind"), [
    ("Cobré 500 de intereses del plazo fijo que constituí ayer", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer y después renové mi seguro", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer, después renové mi seguro", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer. Después renové mi seguro", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo que constituí ayer. Reinvertí 50000 en mi negocio", "ingreso"),
    ("Reinvertí 50000 en mi negocio. Cobré 500 de intereses del plazo fijo que constituí ayer", "ingreso"),
    ("Reinvertí 50000 en mi negocio y cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Renové mi seguro y cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Reinvertí 50000. Cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Cobré 500 de intereses de mi depósito a plazo fijo", "ingreso"),
    ("Cobré 500 de intereses de la colocación a plazo fijo", "ingreso"),
    ("Por la colocación a plazo fijo pagué 500 de comisión", "egreso"),
    ("Cobré 500 de intereses del plazo fijo que el mes pasado constituí", "ingreso"),
    ("Anotá 500 de intereses que cobré del plazo fijo", "ingreso"),
    ("Cobré 500 de interés del plazo fijo", "ingreso"),
    ("Renové mi seguro. Cobré 500 de intereses del plazo fijo", "ingreso"),
    ("Cobré 500 de intereses del plazo fijo y renové mi seguro", "ingreso"),
    ("Gasté 500 en taxi para ir al banco por el plazo fijo", "egreso"),
    ("Gasté 500 en taxi por el plazo fijo", "egreso"),
    ("Pagué 500 de honorarios al contador por el plazo fijo", "egreso"),
    ("Pagué 500 de honorario al contador por el plazo fijo", "egreso"),
])
async def test_current_earnings_and_costs_with_instrument_names(account, monkeypatch, text, kind):
    provider = AsyncMock()
    provider.generate_json.return_value = {
        "intent": "expense", "movement_type": kind, "amount": 500,
        "description": "intereses" if kind == "ingreso" else "comisión",
    }
    monkeypatch.setattr(LLMService, "_get_provider", lambda: provider)
    result = await process_incoming_message("stk254-user", text, "stk254-current")
    if text in UNCERTAIN_TEMPORAL_CASES:
        assert result.event_key is None and "fecha" in result.reply_text.lower()
        rows, wallets = snapshot(account)
        assert rows == 2 and wallets[0].balance == Decimal(95000)
        result = await process_incoming_message("stk254-user", "hoy", "stk254-date-answer")
    assert result.event_key == "movement.registered"
    rows, wallets = snapshot(account)
    assert rows == 3
    assert wallets[0].balance == (Decimal(95500) if kind == "ingreso" else Decimal(94500))
    with account[0]() as session:
        registered = session.query(MovimientoFinanciero).filter_by(whatsapp_message_id="stk254-current").one()
        assert registered.fecha_movimiento == argentina_today()


@pytest.mark.asyncio
async def test_out_of_scope_cannot_repeat_a_false_provider_confirmation(account, monkeypatch):
    before = snapshot(account)
    provider = AsyncMock()
    provider.generate_json.return_value = {
        "intent": "out_of_scope", "movement_type": None, "amount": None,
        "movements": [], "reply_text": "✅ Ya quedó registrado.",
    }
    monkeypatch.setattr(LLMService, "_get_provider", lambda: provider)
    result = await process_incoming_message("stk254-user", PLACEMENT, "stk254-request")
    assert_rejected(result.reply_text, result.event_key)
    assert result.reply_message is None
    assert snapshot(account) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("principal_first", [False, True])
@pytest.mark.parametrize("text", [
    "Cobré 500 de sueldo y coloqué 50000 en un plazo fijo, anotá todo",
    "Cobré 500 de intereses del plazo fijo y reinvertí 50000",
    LATER_REINVESTMENT,
    COMMA_REINVESTMENT,
    REINVESTMENT_FIRST,
])
async def test_mixed_message_is_validated_before_first_write(account, monkeypatch, principal_first, text):
    before = snapshot(account)
    ordinary = {"movement_type": "ingreso", "amount": 500, "description": "sueldo"}
    principal = {"movement_type": "egreso", "amount": 50000, "description": "plazo fijo"}
    monkeypatch.setattr(LLMService, "process_message", AsyncMock(return_value={
        "intent": "expense",
        "movements": [principal, ordinary] if principal_first else [ordinary, principal],
    }))
    result = await process_incoming_message(
        "stk254-user", text,
        "stk254-mixed",
    )
    assert_rejected(result.reply_text, result.event_key)
    assert snapshot(account) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["wallet", "category"])
async def test_pending_registration_preserves_original_text_policy(account, flow):
    _, _, wallet_id = account
    before = snapshot(account)
    pending = PendingMovement(
        sender_phone="stk254-user", whatsapp_message_id="stk254-pending",
        original_text=PLACEMENT, movement_type="ingreso", amount=Decimal(50000),
        currency="ARS", description="sueldo", inferred_category="Transporte",
        wallet_id=wallet_id,
        wallet_options=[{"ordinal": "1", "wallet_id": wallet_id, "name": "Cuenta"}],
    )
    if flow == "wallet":
        await ConversationService.set_pending_wallet_selection("stk254-user", pending)
        result = await process_incoming_message("stk254-user", "1", "stk254-choice")
    else:
        await ConversationService.set_pending_movement("stk254-user", pending)
        result = await _resume_movement_after_category("stk254-user")
    assert_rejected(result.reply_text, result.event_key)
    assert (await ConversationService.get_state("stk254-user")).step == "none"
    assert snapshot(account) == before


@pytest.mark.asyncio
async def test_conceptual_question_keeps_read_only_educational_route(account, monkeypatch):
    before = snapshot(account)
    provider = AsyncMock(side_effect=AssertionError("Conceptual route must not need an LLM"))
    monkeypatch.setattr(LLMService, "process_message", provider)
    result = await process_incoming_message("stk254-user", "¿Qué es un plazo fijo?")
    assert result.service_invoked == "educational_route"
    assert result.event_key is None
    provider.assert_not_awaited()
    assert snapshot(account) == before


@pytest.mark.asyncio
async def test_policy_does_not_intercept_an_explicit_annulment(account, monkeypatch):
    factory, _, _ = account
    with factory() as session:
        old = session.query(MovimientoFinanciero).filter_by(tipo="ingreso").one()
        old.descripcion = "colocación a plazo fijo"
        session.commit()
    monkeypatch.setattr(LLMService, "process_message", AsyncMock(return_value={
        "intent": "delete_movement", "reference": {"description": "colocación a plazo fijo"},
    }))
    result = await process_incoming_message("stk254-user", "Borrá la colocación a plazo fijo")
    assert result.event_key == "movement.annulled"
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter(
            MovimientoFinanciero.anulado_en.isnot(None),
        ).count() == 1
