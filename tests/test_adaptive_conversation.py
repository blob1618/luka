"""Real persistence through the conversational correction and selection paths."""

import uuid
import calendar
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.budget as budget_module
import app.services.dispatcher as dispatcher_module
import app.services.finance as finance_module
import app.services.limit as limit_module
import app.models.database as database_module
from app.models.database import Base, Categoria, LimiteCategoria, MovimientoFinanciero, Usuario
from app.services.dispatcher import _limit_month_label, process_incoming_message
from app.services.onboarding import OnboardingDecision, OnboardingResult


@pytest.fixture()
def conversation_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)
    for module in (finance_module, budget_module, limit_module, dispatcher_module):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(database_module, "SessionLocal", factory)
    monkeypatch.setattr(
        dispatcher_module.OnboardingService, "prepare_whatsapp_message",
        lambda phone: OnboardingResult(OnboardingDecision.KNOWN_USER),
    )
    monkeypatch.setattr(dispatcher_module, "_update_ultimo_mensaje", lambda phone: None)
    monkeypatch.setattr(dispatcher_module.ConversationFlowRuntime, "abandon", AsyncMock())
    monkeypatch.setattr(dispatcher_module.ConversationFlowRuntime, "render_event", AsyncMock(return_value=None))

    state = {"recent": None, "last_movement": None, "last_limit": None,
             "pending_delete": None, "pending_selection": None}

    def getter(name):
        async def get(phone):
            return state[name]
        return get

    def setter(name):
        async def set_value(phone, value):
            state[name] = value
        return set_value

    def clearer(name):
        async def clear(phone):
            state[name] = None
        return clear

    service = dispatcher_module.ConversationService
    for method in (
        "is_awaiting_rename", "is_awaiting_reminder_data",
        "is_awaiting_limit_year_confirmation", "is_awaiting_limit_category_confirmation",
        "is_awaiting_limit_data", "is_awaiting_limit_delete_category",
        "is_awaiting_category_confirmation", "is_awaiting_compensation_confirmation",
    ):
        monkeypatch.setattr(service, method, AsyncMock(return_value=False))
    monkeypatch.setattr(service, "is_awaiting_limit_month_selection", AsyncMock(side_effect=lambda phone: state["pending_delete"] is not None))
    for method, name in (
        ("get_recent_items", "recent"), ("get_last_movement", "last_movement"),
        ("get_last_limit", "last_limit"), ("get_pending_limit_delete", "pending_delete"),
        ("get_pending_selection", "pending_selection"),
    ):
        monkeypatch.setattr(service, method, AsyncMock(side_effect=getter(name)))
    for method, name in (
        ("set_recent_items", "recent"), ("set_last_movement", "last_movement"),
        ("set_last_limit", "last_limit"), ("set_pending_limit_delete", "pending_delete"),
        ("set_pending_selection", "pending_selection"),
    ):
        monkeypatch.setattr(service, method, AsyncMock(side_effect=setter(name)))
    monkeypatch.setattr(service, "clear_state", AsyncMock(side_effect=clearer("pending_delete")))
    monkeypatch.setattr(service, "clear_last_movement", AsyncMock(side_effect=clearer("last_movement")))
    monkeypatch.setattr(service, "clear_last_limit", AsyncMock(side_effect=clearer("last_limit")))
    monkeypatch.setattr(service, "clear_pending_selection", AsyncMock(side_effect=clearer("pending_selection")))

    session = factory()
    user = Usuario(nombre="Usuario", email=f"{uuid.uuid4()}@example.com", whatsapp_id="5491111111111")
    session.add(user)
    session.commit()
    try:
        yield session, user, state
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def category(session, user, name):
    row = Categoria(usuario_id=user.id, nombre=name, es_default=False, esta_eliminado=False)
    session.add(row)
    session.commit()
    return row


def period(offset: int = 0):
    """(month, year) relative to the current month, so these tests don't rot."""
    today = date.today()
    index = today.year * 12 + today.month - 1 + offset
    return index % 12 + 1, index // 12


def limit(session, user, cat, offset, amount):
    month, year = period(offset)
    row = LimiteCategoria(
        usuario_id=user.id, categoria_id=cat.id, cantidad_max=Decimal(amount),
        inicio_periodo=date(year, month, 1),
        fin_periodo=date(year, month, calendar.monthrange(year, month)[1]),
        moneda="ARS",
    )
    session.add(row)
    session.commit()
    return row


@pytest.mark.asyncio
async def test_wrong_llm_new_expense_is_routed_to_correction_then_annulment(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    category(session, user, "Comida")
    responses = iter([
        {"intent": "expense", "movement_type": "egreso", "amount": 10000,
         "currency": "ARS", "description": "pizza", "category": "Comida"},
        {"intent": "expense", "movement_type": "egreso", "amount": 13000,
         "currency": "ARS", "description": "supermercado", "category": "Comida"},
        {"intent": "out_of_scope", "reply_text": "No puedo borrar movimientos."},
    ])
    monkeypatch.setattr(dispatcher_module.LLMService, "process_message", AsyncMock(side_effect=lambda *args, **kwargs: next(responses)))
    phone = user.whatsapp_id

    first = await process_incoming_message(phone, "Compré pizza por 10000", "wamid.pizza")
    corrected = await process_incoming_message(phone, "Era por 13000 en realidad", "wamid.correct")
    session.expire_all()
    rows = session.query(MovimientoFinanciero).all()
    assert "Registré" in first.reply_text
    assert "Corregí pizza" in corrected.reply_text
    assert corrected.event_key == "movement.updated"
    assert len(rows) == 1
    assert rows[0].cantidad == 13000
    assert rows[0].descripcion == "pizza"

    removed = await process_incoming_message(phone, "Fue un error, borrá ese movimiento", "wamid.delete")
    assert "Eliminé pizza" in removed.reply_text
    assert removed.event_key == "movement.annulled"
    assert finance_module.FinanceService.query_movements(user.id).movements == []


@pytest.mark.asyncio
async def test_amount_correction_recomputes_budget_without_duplicate_expense(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    food = category(session, user, "Comida")
    limit(session, user, food, 0, "40000")
    responses = iter([
        {"intent": "expense", "movement_type": "egreso", "amount": 40000,
         "currency": "ARS", "description": "almacén", "category": "Comida"},
        {"intent": "expense", "movement_type": "egreso", "amount": 10000,
         "currency": "ARS", "description": "pizza", "category": "Comida"},
        {"intent": "expense", "movement_type": "egreso", "amount": 13000,
         "currency": "ARS", "description": "supermercado", "category": "Comida"},
    ])
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(side_effect=lambda *args, **kwargs: next(responses)),
    )
    phone = user.whatsapp_id
    await process_incoming_message(phone, "Compré en almacén por 40000", "wamid.almacen")
    await process_incoming_message(phone, "Compré pizza por 10000", "wamid.pizza")
    corrected = await process_incoming_message(phone, "Era por 13000 en realidad", "wamid.fix")
    assert "53.000,00" in corrected.reply_text
    assert "63.000,00" not in corrected.reply_text
    assert session.query(MovimientoFinanciero).count() == 2


@pytest.mark.asyncio
async def test_category_correction_uses_existing_category_only(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    food = category(session, user, "Comida")
    transport = category(session, user, "Transporte")
    responses = iter([
        {"intent": "expense", "movement_type": "egreso", "amount": 5000,
         "currency": "ARS", "description": "uber", "category": "Comida"},
        {"intent": "update_movement", "reference": "last_registered",
         "changes": {"category": "Transporte"}},
        {"intent": "update_movement", "reference": "last_registered",
         "changes": {"category": "Inventada"}},
    ])
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(side_effect=lambda *args, **kwargs: next(responses)),
    )
    phone = user.whatsapp_id
    await process_incoming_message(phone, "Pagué uber 5000", "wamid.uber")
    changed = await process_incoming_message(phone, "Cambiá la categoría a transporte")
    refused = await process_incoming_message(phone, "Mejor categoría inventada")
    assert "Transporte" in changed.reply_text
    assert "No encontré esa categoría activa" in refused.reply_text
    session.expire_all()
    movement = session.query(MovimientoFinanciero).one()
    assert movement.categoria_id == transport.id and movement.categoria_id != food.id
    assert session.query(Categoria).count() == 2


@pytest.mark.asyncio
async def test_edit_listed_limit_without_recent_creation(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    cat = category(session, user, "Transporte")
    original = limit(session, user, cat, 0, "20000")
    target = _limit_month_label(*period(1))
    responses = iter([
        {"intent": "list_limits"},
        {"intent": "change_limit", "limit_category": "Transporte",
         "limit_month": period(1)[0],
         "changes": {"target_month": period(1)[0]}},
    ])
    monkeypatch.setattr(dispatcher_module.LLMService, "process_message", AsyncMock(side_effect=lambda *args, **kwargs: next(responses)))
    phone = user.whatsapp_id
    listed = await process_incoming_message(phone, "Límites")
    changed = await process_incoming_message(phone, f"Cambiá el límite de transporte para {target}")
    session.expire_all()
    assert "Transporte" in listed.reply_text
    assert target in changed.reply_text
    assert session.query(LimiteCategoria).one().id == original.id
    assert session.query(LimiteCategoria).one().inicio_periodo.month == period(1)[0]


@pytest.mark.asyncio
async def test_limit_edit_asks_source_month_when_category_has_multiple(conversation_db, monkeypatch):
    session, user, state = conversation_db
    transport = category(session, user, "Transporte")
    current = limit(session, user, transport, 0, "20000")
    later = limit(session, user, transport, 2, "30000")
    target = _limit_month_label(*period(1))
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(return_value={
            "intent": "change_limit", "reference": {"category": "Transporte"},
            "changes": {"target_month": period(1)[0]},
        }),
    )
    question = await process_incoming_message(
        user.whatsapp_id, f"Cambiá el límite de transporte para {target}"
    )
    assert _limit_month_label(*period(0)) in question.reply_text
    assert _limit_month_label(*period(2)) in question.reply_text
    assert state["pending_selection"] is not None
    reply = await process_incoming_message(
        user.whatsapp_id, _limit_month_label(*period(0))
    )
    assert target in reply.reply_text
    session.expire_all()
    assert session.get(LimiteCategoria, current.id).inicio_periodo.month == period(1)[0]
    assert session.get(LimiteCategoria, later.id).inicio_periodo.month == period(2)[0]


@pytest.mark.asyncio
async def test_both_deletes_only_the_shown_food_limits(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    food = category(session, user, "Comida")
    transport = category(session, user, "Transporte")
    limit(session, user, food, 0, "40000")
    limit(session, user, food, 1, "40000")
    keep = limit(session, user, transport, 0, "20000")
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(return_value={"intent": "delete_limit", "limit_category": "Comida"}),
    )
    asked = await process_incoming_message(user.whatsapp_id, "Borrá el límite de comida")
    removed = await process_incoming_message(user.whatsapp_id, "Ambos")
    assert _limit_month_label(*period(0)) in asked.reply_text
    assert _limit_month_label(*period(1)) in asked.reply_text
    assert "2 límites" in removed.reply_text
    assert removed.event_key == "limit.bulk_deleted"
    assert session.query(LimiteCategoria).one().id == keep.id


@pytest.mark.asyncio
async def test_two_named_months_delete_only_pending_limits(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    food = category(session, user, "Comida")
    transport = category(session, user, "Transporte")
    limit(session, user, food, 0, "40000")
    limit(session, user, food, 1, "40000")
    keep = limit(session, user, transport, 0, "20000")
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(return_value={"intent": "delete_limit", "limit_category": "Comida"}),
    )
    await process_incoming_message(user.whatsapp_id, "Borrá el límite de comida")
    months = f"{_limit_month_label(*period(0))} y {_limit_month_label(*period(1))}"
    result = await process_incoming_message(user.whatsapp_id, months)
    assert "2 límites" in result.reply_text
    assert session.query(LimiteCategoria).one().id == keep.id


@pytest.mark.asyncio
async def test_ambiguous_vegetables_requires_selection_before_annulment(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    food = category(session, user, "Comida")
    for amount in (5000, 7000):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, categoria_id=food.id, tipo="egreso",
            cantidad=Decimal(amount), moneda="ARS", descripcion="verduras",
            origen="whatsapp_text",
        ))
    session.commit()
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(return_value={"intent": "delete_movement", "reference": {"description": "verduras"}}),
    )
    asked = await process_incoming_message(user.whatsapp_id, "Borrá el movimiento de verduras")
    assert "Encontré varios" in asked.reply_text
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 0
    removed = await process_incoming_message(user.whatsapp_id, "El segundo")
    assert "Eliminé verduras" in removed.reply_text
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 1


@pytest.mark.asyncio
async def test_delete_named_movement_after_listing(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    food = category(session, user, "Comida")
    for description in ("verduras", "pizza"):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, categoria_id=food.id, tipo="egreso",
            cantidad=Decimal(5000), moneda="ARS", descripcion=description,
            origen="whatsapp_text",
        ))
    session.commit()
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(side_effect=[
            {"intent": "query_movements"},
            {"intent": "delete_movement", "reference": {"description": "verduras"}},
        ]),
    )
    listed = await process_incoming_message(
        user.whatsapp_id, "mostrame mis movimientos"
    )
    removed = await process_incoming_message(user.whatsapp_id, "Borrá el movimiento de verduras")
    assert "verduras" in listed.reply_text
    assert "Eliminé verduras" in removed.reply_text
    visible = finance_module.FinanceService.query_movements(user.id).movements
    assert [item.descripcion for item in visible] == ["pizza"]


@pytest.mark.asyncio
async def test_natural_movement_queries_list_all_and_only_expenses(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    for kind, description in (("egreso", "ventilador"), ("ingreso", "sueldo")):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, tipo=kind, cantidad=Decimal(1000), moneda="ARS",
            descripcion=description, fecha_movimiento=date(2026, 9, 17), origen="whatsapp_text",
        ))
    session.commit()
    llm = AsyncMock(
        side_effect=[
            {"intent": "query_movements", "movement_type": None},
            {"intent": "query_movements", "movement_type": "egreso"},
        ]
    )
    monkeypatch.setattr(dispatcher_module.LLMService, "process_message", llm)
    all_reply = await process_incoming_message(
        user.whatsapp_id, "mostrame mis movimientos"
    )
    expense_reply = await process_incoming_message(
        user.whatsapp_id, "mostrame solo mis egresos"
    )
    assert "ventilador" in all_reply.reply_text and "sueldo" in all_reply.reply_text
    assert "ventilador" in expense_reply.reply_text and "sueldo" not in expense_reply.reply_text
    assert llm.await_count == 2


@pytest.mark.asyncio
async def test_delete_last_two_shown_movements_atomically(conversation_db, monkeypatch):
    session, user, state = conversation_db
    for index, description in enumerate(("tv", "ventilador", "camisa")):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, tipo="egreso", cantidad=Decimal(1000 + index),
            moneda="ARS", descripcion=description,
            fecha_movimiento=date(2026, 9, 17 - index), origen="whatsapp_text",
        ))
    session.commit()
    monkeypatch.setattr(
        dispatcher_module.LLMService,
        "process_message",
        AsyncMock(
            side_effect=[
                {"intent": "query_movements", "movement_type": None},
                {"intent": "out_of_scope"},
            ]
        ),
    )
    await process_incoming_message(user.whatsapp_id, "mostrame mis movimientos")
    shown_ids = [item["id"] for item in state["recent"].items]
    reply = await process_incoming_message(user.whatsapp_id, "Borrá los últimos dos")
    assert "Eliminé 2 movimientos" in reply.reply_text
    session.expire_all()
    annulled = {str(item.id) for item in session.query(MovimientoFinanciero)
               if item.anulado_en is not None}
    assert annulled == set(shown_ids[:2])


@pytest.mark.asyncio
async def test_delete_two_named_movements_even_if_llm_references_one(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    for description in ("tv", "ventilador", "camisa"):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, tipo="egreso", cantidad=Decimal(1000),
            moneda="ARS", descripcion=description,
            fecha_movimiento=date(2026, 9, 17), origen="whatsapp_text",
        ))
    session.commit()
    monkeypatch.setattr(
        dispatcher_module.LLMService,
        "process_message",
        AsyncMock(
            side_effect=[
                {"intent": "query_movements", "movement_type": None},
                {"intent": "out_of_scope"},
            ]
        ),
    )
    await process_incoming_message(user.whatsapp_id, "mostrame mis movimientos")
    reply = await process_incoming_message(user.whatsapp_id, "Borrá ventilador y tv")
    assert "Eliminé 2 movimientos" in reply.reply_text
    session.expire_all()
    annulled = {item.descripcion for item in session.query(MovimientoFinanciero)
               if item.anulado_en is not None}
    assert annulled == {"ventilador", "tv"}


@pytest.mark.asyncio
async def test_batch_delete_rejects_stale_snapshot_without_partial_change(conversation_db):
    session, user, _ = conversation_db
    for description in ("tv", "ventilador"):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, tipo="egreso", cantidad=Decimal(1000),
            moneda="ARS", descripcion=description,
            fecha_movimiento=date(2026, 9, 17), origen="whatsapp_text",
        ))
    session.commit()
    selected = dispatcher_module._movement_context_items(
        finance_module.FinanceService.find_movement_candidates(user.whatsapp_id)
    )
    row = session.query(MovimientoFinanciero).filter_by(descripcion="tv").one()
    row.cantidad = Decimal(2000)
    session.commit()
    result = finance_module.FinanceService.annul_movements(user.whatsapp_id, selected)
    assert result.status == "stale_context"
    session.expire_all()
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 0


@pytest.mark.asyncio
async def test_pending_movement_selection_accepts_ambos(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    for amount in (5000, 7000):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, tipo="egreso", cantidad=Decimal(amount),
            moneda="ARS", descripcion="verduras",
            fecha_movimiento=date(2026, 9, 17), origen="whatsapp_text",
        ))
    session.commit()
    monkeypatch.setattr(dispatcher_module.LLMService, "process_message", AsyncMock(
        return_value={"intent": "delete_movement", "reference": {"description": "verduras"}}
    ))
    asked = await process_incoming_message(user.whatsapp_id, "Borrá el movimiento de verduras")
    assert "Encontré varios" in asked.reply_text
    reply = await process_incoming_message(user.whatsapp_id, "Ambos")
    assert "Eliminé 2 movimientos" in reply.reply_text
    session.expire_all()
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 2


@pytest.mark.asyncio
async def test_multi_name_delete_never_removes_only_one_match(conversation_db, monkeypatch):
    session, user, _ = conversation_db
    session.add(MovimientoFinanciero(
        usuario_id=user.id, tipo="egreso", cantidad=Decimal(1000),
        moneda="ARS", descripcion="ventilador",
        fecha_movimiento=date(2026, 9, 17), origen="whatsapp_text",
    ))
    session.commit()
    monkeypatch.setattr(dispatcher_module.LLMService, "process_message", AsyncMock(
        return_value={"intent": "delete_movement", "reference": {"description": "ventilador"}}
    ))
    reply = await process_incoming_message(user.whatsapp_id, "Borrá ventilador y tv")
    assert "No pude identificar" in reply.reply_text
    session.expire_all()
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 0


def test_batch_delete_rejects_other_users_movement_atomically(conversation_db):
    session, user, _ = conversation_db
    other = Usuario(nombre="Otro", email=f"{uuid.uuid4()}@example.com", whatsapp_id="5491222222222")
    session.add(other)
    session.flush()
    for owner, description in ((user, "tv"), (other, "ventilador")):
        session.add(MovimientoFinanciero(
            usuario_id=owner.id, tipo="egreso", cantidad=Decimal(1000),
            moneda="ARS", descripcion=description,
            fecha_movimiento=date(2026, 9, 17), origen="whatsapp_text",
        ))
    session.commit()
    selected = dispatcher_module._movement_context_items([
        finance_module.FinanceService.find_movement_candidates(owner.whatsapp_id)[0]
        for owner in (user, other)
    ])
    result = finance_module.FinanceService.annul_movements(user.whatsapp_id, selected)
    assert result.status == "not_found"
    session.expire_all()
    assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 0


@pytest.mark.asyncio
async def test_multi_movement_message_retains_distinct_ids_for_ordinal_correction(conversation_db, monkeypatch):
    session, user, state = conversation_db
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(side_effect=[
            {"intent": "expense", "movement_type": "egreso", "movements": [
                {"movement_type": "egreso", "amount": 1000, "currency": "ARS",
                 "description": "pan", "category": None},
                {"movement_type": "egreso", "amount": 2000, "currency": "ARS",
                 "description": "leche", "category": None},
            ]},
            {"intent": "update_movement", "changes": {"amount": 2500}},
        ]),
    )
    first = await process_incoming_message(user.whatsapp_id, "Compré pan 1000 y leche 2000", "wamid.multi")
    assert "2 movimientos" in first.reply_text
    assert len(state["recent"].items) == 2
    assert state["last_movement"] is None
    edited = await process_incoming_message(user.whatsapp_id, "El segundo era por 2500")
    assert "Corregí leche" in edited.reply_text
    session.expire_all()
    rows = {row.descripcion: row for row in session.query(MovimientoFinanciero).all()}
    assert len(rows) == 2
    assert rows["pan"].cantidad == 1000 and rows["leche"].cantidad == 2500
    assert rows["pan"].whatsapp_message_id != rows["leche"].whatsapp_message_id


@pytest.mark.asyncio
async def test_new_explicit_request_interrupts_ambiguous_selection(conversation_db, monkeypatch):
    session, user, state = conversation_db
    food = category(session, user, "Comida")
    for amount in (5000, 7000):
        session.add(MovimientoFinanciero(
            usuario_id=user.id, categoria_id=food.id, tipo="egreso",
            cantidad=Decimal(amount), moneda="ARS", descripcion="verduras",
            origen="whatsapp_text",
        ))
    session.commit()
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(side_effect=[
            {"intent": "delete_movement", "reference": {"description": "verduras"}},
            {"intent": "expense", "movement_type": "egreso", "amount": 1000,
             "currency": "ARS", "description": "pan", "category": "Comida"},
        ]),
    )
    await process_incoming_message(user.whatsapp_id, "Borrá el movimiento de verduras")
    assert state["pending_selection"] is not None
    reply = await process_incoming_message(user.whatsapp_id, "Compré pan por 1000")
    assert "Registré" in reply.reply_text
    assert state["pending_selection"] is None
    assert session.query(MovimientoFinanciero).count() == 3


@pytest.mark.asyncio
async def test_new_purchase_interrupts_pending_limit_deletion(conversation_db, monkeypatch):
    session, user, state = conversation_db
    food = category(session, user, "Comida")
    limit(session, user, food, 0, "40000")
    limit(session, user, food, 1, "40000")
    monkeypatch.setattr(
        dispatcher_module.LLMService, "process_message",
        AsyncMock(side_effect=[
            {"intent": "delete_limit", "limit_category": "Comida"},
            {"intent": "expense", "movement_type": "egreso", "amount": 1000,
             "currency": "ARS", "description": "pan", "category": "Comida"},
        ]),
    )
    await process_incoming_message(user.whatsapp_id, "Borrá el límite de comida")
    assert state["pending_delete"] is not None
    reply = await process_incoming_message(user.whatsapp_id, "Compré pan por 1000")
    assert "Registré" in reply.reply_text
    assert state["pending_delete"] is None
    assert session.query(LimiteCategoria).count() == 2
