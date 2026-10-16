"""Focal tests for the deterministic read-only educational route (STK-243)."""

import uuid
from contextlib import ExitStack, contextmanager
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.monthly_balance as monthly_balance_module
from app.models.database import (
    Base,
    Categoria,
    LimiteCategoria,
    MovimientoFinanciero,
    Usuario,
)
from app.services.conversation import (
    ConversationService,
    PendingConversationFlow,
    PendingSelection,
)
from app.services.dispatcher import process_incoming_message
from app.services.educational_route import (
    classify_educational_request,
    render_educational_reply,
)
from app.services.investment_guidance import InvestmentGuidanceError
from app.services.monthly_balance import (
    ARGENTINA_TZ,
    MonthlyBalanceResult,
)
from app.services.onboarding import OnboardingDecision, OnboardingResult


OCTOBER_2026 = datetime(2026, 10, 15, 12, 0, tzinfo=ARGENTINA_TZ)
PHONE_A = "5491100000001"
PHONE_B = "5491100000002"


# ---------------------------------------------------------------------------
# Classifier (pure)
# ---------------------------------------------------------------------------


BALANCE_REQUESTS = [
    "¿Cuánto me sobró el mes pasado?",
    "quiero revisar mi sobrante",
    "cuánto me quedó el mes pasado",
    "cuánto ahorré el mes pasado",
    "cuál fue mi balance del mes pasado",
]


@pytest.mark.parametrize("text", BALANCE_REQUESTS)
def test_balance_requests_are_classified_without_a_topic(text):
    request = classify_educational_request(text)

    assert request is not None
    assert request.asks_balance is True
    assert request.topic_id is None


@pytest.mark.parametrize(
    ("text", "topic_id"),
    [
        ("compará plazo fijo y FCI", "comparacion_plazo_fijo_fci"),
        ("¿Qué conviene más, plazo fijo o FCI?", "comparacion_plazo_fijo_fci"),
        ("explícame el plazo fijo", "plazo_fijo_tradicional"),
        ("¿qué es un FCI?", "fci_mercado_dinero"),
        ("contame sobre la reserva para imprevistos", "reserva_imprevistos"),
        ("contame sobre la reserva para emergencias", "reserva_imprevistos"),
        ("quiero ahorrar para una meta", "meta_ahorro"),
    ],
)
def test_explicit_topics_map_to_catalog_ids(text, topic_id):
    request = classify_educational_request(text)

    assert request is not None
    assert request.topic_id == topic_id


@pytest.mark.parametrize(
    ("text", "asks_balance"),
    [
        ("qué puedo hacer con mi excedente", True),
        ("quiero orientación sobre ahorro e inversión", False),
        ("dame opciones de ahorro", False),
        ("quiero empezar a invertir", False),
        ("no sé en qué invertir", False),
    ],
)
def test_general_guidance_requests_are_classified(text, asks_balance):
    request = classify_educational_request(text)

    assert request is not None
    assert request.asks_overview is True
    assert request.asks_balance is asks_balance
    assert request.topic_id is None


@pytest.mark.parametrize(
    "text",
    [
        "sí",
        "confirmar",
        "no",
        "cancelar",
        "1000",
        "5000 pesos",
        "supermercado",
        "comida",
        "gasté 5000 en comida",
        "registrá un gasto de 2000",
        "anotá un ingreso de 50000",
        "cobré el sueldo",
        "compré un plazo fijo",
        "invertí 5000 en un plazo fijo",
        "transferí 5000 a mi caja de ahorro",
        "ahorré 1000 pesos hoy",
        "¿qué es un dashboard?",
        "mostrame mis movimientos",
        "cuánto me queda de presupuesto",
        "mi saldo",
        "mi sobrante",
        "plazo fijo",
        "FCI",
        "mi reserva para imprevistos",
        "cuánto gasté el mes pasado",
        "cuál fue mi balance de este mes",
        "cuánto ahorré en los últimos meses",
        "menú",
        "ayuda",
        "/reset",
    ],
)
def test_short_transactional_and_ambiguous_messages_are_ignored(text):
    assert classify_educational_request(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "cuál fue mi balance del mes actual",
        "cuál fue mi balance mensual de este mes",
        "cuánto ahorré en los últimos meses",
        "quiero ver mi balance del mes en curso",
        "cuánto me quedó en lo que va del mes",
        "mi balance de los últimos meses",
        "cuánto ahorré en varios meses",
    ],
)
def test_open_or_multi_month_requests_fall_through(text):
    assert classify_educational_request(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "¿Cuánto me sobró del presupuesto de comida?",
        "¿Cuánto me sobró del presupuesto de comida el mes pasado?",
        "¿Cuánto me sobró del límite de comida el mes pasado?",
        "¿Cuánto me sobró de la categoría comida?",
        "¿Cuánto me sobró de las categorías de comida el mes pasado?",
    ],
)
def test_category_budget_surplus_is_not_a_monthly_balance_request(text):
    assert classify_educational_request(text) is None


def test_explicit_unsupported_month_does_not_use_last_closed_month_balance():
    assert classify_educational_request("¿Cuánto me sobró en agosto?") is None


def test_open_period_blocks_only_the_balance_calculation_not_static_education():
    topic_request = classify_educational_request(
        "quiero ahorrar para una meta en varios meses"
    )

    assert topic_request is not None
    assert topic_request.topic_id == "meta_ahorro"
    assert topic_request.asks_balance is False

    overview_request = classify_educational_request(
        "¿Qué puedo hacer con mi ahorro en los próximos meses?"
    )

    assert overview_request is not None
    assert overview_request.topic_id is None
    assert overview_request.asks_overview is True
    assert overview_request.asks_balance is False


@pytest.mark.parametrize(
    "text",
    [
        "¿Qué hago con la tarjeta?",
        "¿Qué puedo hacer con esta categoría?",
        "¿Qué puedo hacer con esto?",
    ],
)
def test_nonfinancial_or_dangling_overview_objects_are_ignored(text):
    assert classify_educational_request(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "quiero orientación para entrar al dashboard",
        "quiero entrar al panel avanzado",
        "pasame el link de mi panel",
    ],
)
def test_dashboard_navigation_is_never_an_educational_request(text):
    assert classify_educational_request(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "quiero comprar un plazo fijo",
        "quiero invertir en un plazo fijo",
        "necesito colocar plata en un FCI",
    ],
)
def test_action_intent_on_an_instrument_is_never_educational(text):
    assert classify_educational_request(text) is None


def test_classifier_is_pure_and_deterministic():
    text = "¿Cuánto me sobró el mes pasado?"

    first = classify_educational_request(text)
    second = classify_educational_request(text)

    assert first == second
    assert first is not None
    assert first.topic_id is None


# ---------------------------------------------------------------------------
# Route rendering (patched balance result)
# ---------------------------------------------------------------------------


def _balance_result(status, *, currencies=(), start=date(2026, 9, 1), end=date(2026, 10, 1)):
    return MonthlyBalanceResult(
        status=status,
        period_start=start,
        period_end=end,
        currencies=list(currencies),
    )


def _currency(currency, income, expense, difference):
    return monthly_balance_module.CurrencyBalance(
        currency=currency,
        income=Decimal(income),
        expense=Decimal(expense),
        difference=Decimal(difference),
        movement_count=1,
    )


def test_render_balance_uses_only_the_monthly_balance_result():
    result = _balance_result("ok", currencies=[_currency("ARS", "1000.00", "400.00", "600.00")])

    with patch("app.services.educational_route.query_monthly_balance", return_value=result) as query:
        text = render_educational_reply(PHONE_A, classify_educational_request("¿cuánto me sobró el mes pasado?"))

    query.assert_called_once_with(PHONE_A)
    assert "01/09/2026 al 30/09/2026" in text
    assert "ARS" in text
    assert "diferencia 600,00 (sobrante positivo registrado)" in text


def test_zero_difference_is_not_a_positive_surplus():
    result = _balance_result("zero_difference", currencies=[_currency("ARS", "500.00", "500.00", "0")])

    with patch("app.services.educational_route.query_monthly_balance", return_value=result):
        text = render_educational_reply(PHONE_A, classify_educational_request("¿cuánto me sobró?"))

    assert "No hay un sobrante positivo registrado" in text
    assert "diferencia 0,00 (sin sobrante positivo registrado)" in text
    assert "Hay un sobrante positivo" not in text


def test_negative_difference_is_not_a_positive_surplus():
    result = _balance_result("ok", currencies=[_currency("ARS", "1000.00", "1500.00", "-500.00")])

    with patch("app.services.educational_route.query_monthly_balance", return_value=result):
        text = render_educational_reply(PHONE_A, classify_educational_request("¿cuánto me sobró?"))

    assert "diferencia -500,00 (sin sobrante positivo registrado)" in text
    assert "Hay un sobrante positivo" not in text


def test_statuses_never_become_zero_or_guessed_values():
    for status, expected in [
        ("no_data", "No hay movimientos registrados"),
        ("user_not_found", "No encuentro una cuenta vinculada"),
        ("error", "No pude calcular"),
    ]:
        result = _balance_result(status)
        with patch("app.services.educational_route.query_monthly_balance", return_value=result):
            text = render_educational_reply(PHONE_A, classify_educational_request("¿cuánto me sobró?"))

        assert expected in text
        assert "0,00" not in text
        assert "sobrante positivo registrado" not in text


def test_guidance_topic_uses_the_versioned_catalog():
    request = classify_educational_request("compará plazo fijo y FCI")

    with patch("app.services.educational_route.query_monthly_balance") as query:
        text = render_educational_reply(PHONE_A, request)

    query.assert_not_called()
    assert "comparación educativa" in text
    assert "Límites:" in text
    assert "Revisado: 2026-10-13" in text


def test_general_guidance_uses_the_catalog_overview():
    request = classify_educational_request("quiero orientación sobre ahorro e inversión")

    with patch("app.services.educational_route.query_monthly_balance") as query:
        text = render_educational_reply(PHONE_A, request)

    query.assert_not_called()
    assert "Orientación educativa v1.0.0" in text


def test_guidance_catalog_failure_is_reported_without_guessing():
    request = classify_educational_request("compará plazo fijo y FCI")

    with patch(
        "app.services.educational_route.render_topic",
        side_effect=InvestmentGuidanceError("catálogo roto"),
    ):
        text = render_educational_reply(PHONE_A, request)

    assert "No pude cargar el contenido educativo" in text
    assert "0,00" not in text


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def balance_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(monthly_balance_module, "SessionLocal", factory)
    monkeypatch.setattr(monthly_balance_module, "argentina_now", lambda now=None: OCTOBER_2026)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _add_user(factory, phone):
    with factory() as session:
        user = Usuario(
            id=uuid.uuid4(),
            whatsapp_id=phone,
            nombre="Test User",
            email=f"user_{uuid.uuid4()}@example.com",
        )
        session.add(user)
        session.commit()
        return user.id


def _add_movement(
    factory,
    user_id,
    when,
    *,
    tipo="egreso",
    cantidad="100.00",
    moneda="ARS",
    anulado_en=None,
    categoria_id=None,
):
    with factory() as session:
        session.add(
            MovimientoFinanciero(
                id=uuid.uuid4(),
                usuario_id=user_id,
                categoria_id=categoria_id,
                tipo=tipo,
                cantidad=Decimal(cantidad),
                moneda=moneda,
                descripcion="movimiento",
                fecha_movimiento=when,
                origen="whatsapp_text",
                whatsapp_message_id=f"wamid.{uuid.uuid4()}",
                anulado_en=anulado_en,
            )
        )
        session.commit()


def _add_category(factory, user_id, nombre="Educación"):
    with factory() as session:
        categoria = Categoria(
            id=uuid.uuid4(),
            usuario_id=user_id,
            nombre=nombre,
            es_default=False,
            esta_eliminado=False,
        )
        session.add(categoria)
        session.commit()
        return categoria.id


def _add_limit(
    factory,
    user_id,
    categoria_id,
    *,
    cantidad_max="45000.00",
    inicio=date(2026, 9, 1),
    fin=date(2026, 9, 30),
):
    with factory() as session:
        session.add(
            LimiteCategoria(
                id=uuid.uuid4(),
                usuario_id=user_id,
                categoria_id=categoria_id,
                cantidad_max=Decimal(cantidad_max),
                moneda="ARS",
                inicio_periodo=inicio,
                fin_periodo=fin,
            )
        )
        session.commit()


@contextmanager
def _route_patches(llm_return=None):
    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
        ),
        patch("app.services.dispatcher._update_ultimo_mensaje"),
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value=llm_return if llm_return is not None else {"intent": "greeting"},
        ) as llm,
    ):
        yield llm


WRITE_GUARD_TARGETS = (
    "app.services.dispatcher.FinanceService.register_movement_with_category",
    "app.services.dispatcher.FinanceService.register_movement_from_whatsapp_text",
    "app.services.dispatcher.FinanceService.update_movement",
    "app.services.dispatcher.FinanceService.annul_movement",
    "app.services.dispatcher.FinanceService.annul_movements",
    "app.services.dispatcher.FinanceService.create_category",
    "app.services.dispatcher.FinanceService.delete_category",
    "app.services.dispatcher.LimitService.create_limit",
    "app.services.dispatcher.LimitService.delete_limit",
    "app.services.dispatcher.LimitService.delete_limits_by_ids",
    "app.services.dispatcher.BudgetCompensationService.apply",
)


@contextmanager
def _write_guards():
    guards = []
    with ExitStack() as stack:
        for target in WRITE_GUARD_TARGETS:
            guard = MagicMock(side_effect=AssertionError(f"Escritura prohibida: {target}"))
            stack.enter_context(patch(target, guard))
            guards.append(guard)
        yield guards


def _movement_snapshot(factory):
    with factory() as session:
        return [
            (
                str(row.id),
                str(row.usuario_id),
                str(row.categoria_id) if row.categoria_id is not None else None,
                row.tipo,
                str(row.cantidad),
                row.moneda,
                row.descripcion,
                row.fecha_movimiento,
                row.origen,
                row.whatsapp_message_id,
                row.creado_en,
                row.actualizado_en,
                row.anulado_en,
            )
            for row in session.query(MovimientoFinanciero).order_by(MovimientoFinanciero.id).all()
        ]


def _limit_snapshot(factory):
    with factory() as session:
        return [
            (
                str(row.id),
                str(row.usuario_id),
                str(row.categoria_id),
                str(row.cantidad_max),
                row.moneda,
                row.inicio_periodo,
                row.fin_periodo,
                row.creado_en,
                row.actualizado_en,
            )
            for row in session.query(LimiteCategoria).order_by(LimiteCategoria.id).all()
        ]


@pytest.mark.asyncio
async def test_balance_request_is_read_only_and_never_calls_llm(balance_db):
    user_id = _add_user(balance_db, PHONE_A)
    _add_movement(balance_db, user_id, date(2026, 9, 5), tipo="ingreso", cantidad="1000.00")
    _add_movement(balance_db, user_id, date(2026, 9, 10), tipo="egreso", cantidad="400.00")
    before = _movement_snapshot(balance_db)

    with _route_patches() as llm, _write_guards() as guards:
        result = await process_incoming_message(PHONE_A, "¿Cuánto me sobró el mes pasado?")

    assert result.intent == "educational_route"
    assert result.service_invoked == "educational_route"
    assert result.raw_llm_response is None
    llm.assert_not_awaited()
    for guard in guards:
        guard.assert_not_called()
    assert _movement_snapshot(balance_db) == before
    assert "01/09/2026 al 30/09/2026" in result.reply_text
    assert "diferencia 600,00 (sobrante positivo registrado)" in result.reply_text
    assert "no demuestran que el dinero esté disponible" in result.reply_text
    assert "puedo ampliar con orientación general" in result.reply_text


@pytest.mark.asyncio
async def test_acceptance_positive_surplus_is_read_only_and_isolated(balance_db):
    """STK-244: ARS 100.000 income minus ARS 30.000 expense yields ARS 70.000.

    The synthetic dataset also carries noise that must never reach the answer:
    an annulled row, a row outside the closed period, a movement owned by
    another WhatsApp account and a category/limit row. The fixed ``balance_db``
    date makes the closed period deterministic (September 2026, Argentina time).
    """
    user_a = _add_user(balance_db, PHONE_A)
    user_b = _add_user(balance_db, PHONE_B)
    category_id = _add_category(balance_db, user_a)
    _add_limit(balance_db, user_a, category_id)

    _add_movement(
        balance_db, user_a, date(2026, 9, 5), tipo="ingreso", cantidad="100000.00"
    )
    _add_movement(
        balance_db,
        user_a,
        date(2026, 9, 10),
        tipo="egreso",
        cantidad="30000.00",
        categoria_id=category_id,
    )
    _add_movement(
        balance_db,
        user_a,
        date(2026, 9, 12),
        tipo="ingreso",
        cantidad="999999.00",
        categoria_id=category_id,
        anulado_en=datetime(2026, 9, 13, 12, 0, tzinfo=ARGENTINA_TZ),
    )
    _add_movement(
        balance_db, user_a, date(2026, 8, 5), tipo="ingreso", cantidad="555555.00"
    )
    _add_movement(
        balance_db, user_b, date(2026, 9, 15), tipo="ingreso", cantidad="888888.00"
    )
    movements_before = _movement_snapshot(balance_db)
    limits_before = _limit_snapshot(balance_db)

    with _route_patches() as llm, _write_guards() as guards:
        result = await process_incoming_message(PHONE_A, "¿Cuánto me sobró el mes pasado?")

    assert result.intent == "educational_route"
    assert result.service_invoked == "educational_route"
    assert result.raw_llm_response is None
    llm.assert_not_awaited()
    for guard in guards:
        guard.assert_not_called()
    assert _movement_snapshot(balance_db) == movements_before
    assert _limit_snapshot(balance_db) == limits_before

    reply = result.reply_text
    assert "01/09/2026 al 30/09/2026" in reply
    assert "ingresos 100.000,00, egresos 30.000,00" in reply
    assert "diferencia 70.000,00 (sobrante positivo registrado)" in reply
    assert "Hay un sobrante positivo registrado" in reply
    assert "pueden faltar movimientos no cargados o pagos futuros" in reply
    assert "no demuestran que el dinero esté disponible" in reply

    # The annulled row, the out-of-period row, the foreign account and the
    # budget limit leave no trace in the reply.
    for leaked in ("999.999", "555.555", "888.888", "45.000"):
        assert leaked not in reply


@pytest.mark.asyncio
async def test_educational_request_with_amount_is_read_only(balance_db):
    user_id = _add_user(balance_db, PHONE_A)
    _add_movement(balance_db, user_id, date(2026, 9, 5), tipo="ingreso", cantidad="1000.00")
    before = _movement_snapshot(balance_db)

    with _route_patches() as llm, _write_guards() as guards:
        result = await process_incoming_message(
            PHONE_A, "tengo 100000 de sobrante, ¿qué puedo hacer con eso?"
        )

    assert result.intent == "educational_route"
    assert llm.await_count == 0
    for guard in guards:
        guard.assert_not_called()
    assert _movement_snapshot(balance_db) == before
    assert "Movimientos registrados del mes cerrado" in result.reply_text
    assert "Orientación educativa v1.0.0" in result.reply_text


@pytest.mark.asyncio
async def test_balance_route_isolates_each_phone(balance_db):
    user_a = _add_user(balance_db, PHONE_A)
    user_b = _add_user(balance_db, PHONE_B)
    _add_movement(balance_db, user_a, date(2026, 9, 5), tipo="ingreso", cantidad="1000.00")
    _add_movement(balance_db, user_b, date(2026, 9, 5), tipo="ingreso", cantidad="7777.00")

    with _route_patches():
        result_a = await process_incoming_message(PHONE_A, "cuánto me sobró el mes pasado")
        result_b = await process_incoming_message(PHONE_B, "cuánto me sobró el mes pasado")

    assert "1.000,00" in result_a.reply_text
    assert "7.777,00" not in result_a.reply_text
    assert "7.777,00" in result_b.reply_text
    assert "1.000,00" not in result_b.reply_text


@pytest.mark.asyncio
async def test_multiple_currencies_are_reported_separately(balance_db):
    user_id = _add_user(balance_db, PHONE_A)
    _add_movement(balance_db, user_id, date(2026, 9, 5), tipo="ingreso", cantidad="1000.00", moneda="ARS")
    _add_movement(balance_db, user_id, date(2026, 9, 6), tipo="egreso", cantidad="1500.00", moneda="ARS")
    _add_movement(balance_db, user_id, date(2026, 9, 7), tipo="ingreso", cantidad="100.00", moneda="USD")
    _add_movement(balance_db, user_id, date(2026, 9, 8), tipo="egreso", cantidad="100.00", moneda="USD")

    with _route_patches():
        result = await process_incoming_message(PHONE_A, "¿cuánto me sobró el mes pasado?")

    assert "ARS: ingresos 1.000,00, egresos 1.500,00, diferencia -500,00" in result.reply_text
    assert "USD: ingresos 100,00, egresos 100,00, diferencia 0,00" in result.reply_text
    assert "No hay un sobrante positivo registrado" in result.reply_text
    assert "Hay un sobrante positivo" not in result.reply_text


@pytest.mark.asyncio
async def test_no_data_is_reported_without_inventing_zero(balance_db):
    user_id = _add_user(balance_db, PHONE_A)
    _add_movement(balance_db, user_id, date(2026, 8, 5), tipo="ingreso", cantidad="1000.00")

    with _route_patches():
        result = await process_incoming_message(PHONE_A, "¿cuánto me sobró el mes pasado?")

    assert "No hay movimientos registrados" in result.reply_text
    assert "0,00" not in result.reply_text


@pytest.mark.asyncio
async def test_user_not_found_is_reported_without_guessing(balance_db):
    with _route_patches():
        result = await process_incoming_message(PHONE_A, "¿cuánto me sobró el mes pasado?")

    assert "No encuentro una cuenta vinculada" in result.reply_text
    assert "0,00" not in result.reply_text


@pytest.mark.asyncio
async def test_balance_error_is_reported_without_guessing(balance_db):
    error_result = _balance_result("error")

    with (
        _route_patches(),
        patch(
            "app.services.educational_route.query_monthly_balance",
            return_value=error_result,
        ),
    ):
        result = await process_incoming_message(PHONE_A, "¿cuánto me sobró el mes pasado?")

    assert "No pude calcular" in result.reply_text
    assert "0,00" not in result.reply_text


@pytest.mark.asyncio
async def test_pending_state_is_preserved_on_the_educational_path(balance_db, fake_redis):
    _add_user(balance_db, PHONE_A)
    pending_flow = PendingConversationFlow(
        flow_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        event_key="budget.threshold_crossed",
        node_id="node-1",
        variables={"category": "pan"},
    )
    pending_selection = PendingSelection(
        intent="delete_movement",
        entity="movement",
        items=[{"id": "mov-1", "descripcion": "pan"}],
    )
    await ConversationService.set_pending_conversation_flow(PHONE_A, pending_flow)
    await ConversationService.set_pending_selection(PHONE_A, pending_selection)
    before = dict(fake_redis._storage)

    with _route_patches():
        result = await process_incoming_message(PHONE_A, "¿cuánto me sobró el mes pasado?")

    assert result.intent == "educational_route"
    assert dict(fake_redis._storage) == before
    assert await ConversationService.get_pending_conversation_flow(PHONE_A) is not None
    assert await ConversationService.get_pending_selection(PHONE_A) == pending_selection


@pytest.mark.asyncio
async def test_abandon_is_skipped_only_for_recognized_education():
    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(OnboardingDecision.KNOWN_USER),
        ),
        patch("app.services.dispatcher._update_ultimo_mensaje"),
        patch(
            "app.services.dispatcher.ConversationFlowRuntime.abandon",
            new_callable=AsyncMock,
        ) as abandon,
        patch(
            "app.services.dispatcher.LLMService.process_message",
            new_callable=AsyncMock,
            return_value={"intent": "greeting", "reply_text": "¡Hola!"},
        ),
        patch(
            "app.services.educational_route.query_monthly_balance",
            return_value=_balance_result("no_data"),
        ),
    ):
        await process_incoming_message(PHONE_A, "¿cuánto me sobró el mes pasado?")
        abandon.assert_not_awaited()

        await process_incoming_message(PHONE_A, "sí")
        assert abandon.await_count == 1


@pytest.mark.asyncio
async def test_onboarding_gate_precedes_the_educational_route(balance_db):
    text = "¿cuánto me sobró el mes pasado?"
    assert classify_educational_request(text) is not None

    with (
        patch(
            "app.services.dispatcher.OnboardingService.prepare_whatsapp_message",
            return_value=OnboardingResult(
                OnboardingDecision.SEND_INVITATION,
                registration_url="https://example.com/registro",
                invitation_ttl_minutes=30,
            ),
        ),
        patch(
            "app.services.dispatcher.ConversationFlowRuntime.abandon",
            new_callable=AsyncMock,
        ) as abandon,
        patch("app.services.dispatcher.LLMService.process_message", new_callable=AsyncMock) as llm,
        patch(
            "app.services.educational_route.query_monthly_balance"
        ) as query,
    ):
        result = await process_incoming_message(PHONE_A, text)

    assert result.service_invoked == "onboarding"
    llm.assert_not_awaited()
    query.assert_not_called()
    # The classified request was not ultimately accepted, so the old cleanup runs.
    abandon.assert_awaited_once_with(PHONE_A)
