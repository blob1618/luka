"""Published flows exercise the real limit service, database and conversation state."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.whatsapp import WhatsAppReplyButtons, WhatsAppText
from app.models import database
from app.models.database import Base, Categoria, LimiteCategoria, Usuario
from app.services import budget, category_creation, conversation_flow, dispatcher, finance, limit, onboarding
from app.services.conversation import ConversationService
from app.services.conversation_flow import ConversationFlowService
from app.services.conversation_flow_contract import (
    ConversationFlowDefinitionInvalid,
    validate_flow_definition,
)
from app.services.dispatcher import (
    process_incoming_interactive_reply,
    process_incoming_message,
)
from app.services.limit import LimitResult
from app.services.limit_flow_journey import LIMIT_JOURNEY, limit_journey_definition
from app.services.category_creation import category_creation_definition


PHONE = "541100000099"


def definition(body, actions=()):
    node = {"id": "message", "body": body, "type": "text"}
    if actions:
        node.update(type="reply_button", options=[
            {"id": action, "title": label, "action": action}
            for action, label in actions
        ])
    return {"start_node": "message", "nodes": [node]}


@pytest.fixture
def environment(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    for module in (database, limit, budget, onboarding, conversation_flow, dispatcher, finance, category_creation):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    with sessions() as session:
        user = Usuario(nombre="Demo", email="limit-demo@example.test", whatsapp_id=PHONE)
        session.add(user)
        session.flush()
        session.add(Categoria(nombre="Comida", usuario_id=user.id))
        session.commit()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 26, 12, tzinfo=tz)

    monkeypatch.setattr(limit, "datetime", FixedDatetime)
    monkeypatch.setattr(dispatcher, "datetime", FixedDatetime)
    llm = AsyncMock()
    monkeypatch.setattr(dispatcher.LLMService, "process_message", llm)
    definitions = {
        "limit.listed": definition("{summary}", [("start_limit", "Crear límite")]),
        "limit.missing_data": definition(
            "Para continuar, escribí {missing_field}.",
            [("cancel_pending_operation", "Cancelar")],
        ),
        "limit.year_confirmation": definition(
            "¿Lo aplicamos en {year}?",
            [("confirm_limit_year", "Sí, continuar"), ("reject_limit", "Cancelar")],
        ),
        "limit.created": definition("Creado: {category}, {amount} {currency}, {period}."),
        "limit.updated": definition("Actualizado: {category}, {amount} {currency}, {period}."),
    }
    journey = limit_journey_definition()
    for event, payload in definitions.items():
        node = payload["nodes"][0]
        node["id"] = journey["event_nodes"][event]
        for option in node.get("options", []):
            option["id"] = node["id"] + "-" + option["id"]
        journey["nodes"] = [node if n["id"] == node["id"] else n for n in journey["nodes"]]
    flow = ConversationFlowService.create(
        slug="limites", name="Crear y editar límites", event_key=LIMIT_JOURNEY, definition=journey,
    )
    ConversationFlowService.publish(flow.id)
    category_flow = ConversationFlowService.create(
        slug="crear-categoria", name="Crear categoría", event_key="category.confirmation_required",
        definition=category_creation_definition(),
    )
    ConversationFlowService.publish(category_flow.id)
    yield sessions, llm
    engine.dispose()


async def say(llm, text, **data):
    llm.return_value = {"intent": "create_limit", **data}
    return await process_incoming_message(PHONE, text)


async def click(result, index=0):
    assert isinstance(result.reply_message, WhatsAppReplyButtons)
    return await process_incoming_interactive_reply(
        sender_phone=PHONE,
        option_id=result.reply_message.buttons[index].id,
        reply_type="button_reply",
    )


@pytest.mark.asyncio
async def test_start_button_collects_data_confirms_and_persists_once(environment):
    sessions, llm = environment
    listing = await say(llm, "ver mis límites", intent="list_limits")
    assert listing.event_key == "limit.listed"
    start = await click(listing)
    assert start.event_key == "limit.started"
    assert "Indicá categoría, monto y mes" in start.reply_message.body
    llm.assert_awaited_once()  # The button starts directly, without LLM interpretation.
    amount = await say(llm, "80000", limit_amount=80000)
    assert amount.event_variables == {"missing_field": "la categoría"}
    category = await say(llm, "Mascotas", limit_category="Mascotas")
    assert category.event_key == "category.confirmation_required"
    with sessions() as session:
        assert session.scalar(select(LimiteCategoria)) is None
        assert session.scalar(select(Categoria).where(Categoria.nombre == "Mascotas")) is None
    created = await click(category)
    assert created.event_key == "limit.created"
    assert isinstance(created.reply_message, WhatsAppText)
    assert created.reply_message.body.startswith("Creado: Mascotas, 80.000,00 ARS")
    assert await ConversationService.get_pending_limit(PHONE) is None
    with sessions() as session:
        rows = session.scalars(select(LimiteCategoria)).all()
        assert len(rows) == 1
        assert rows[0].cantidad_max == Decimal("80000")
    stale = await click(category)
    assert stale.reply_message is None
    with sessions() as session:
        assert len(session.scalars(select(LimiteCategoria)).all()) == 1


@pytest.mark.asyncio
async def test_year_button_chains_to_category_button_then_success(environment):
    sessions, llm = environment
    year = await say(llm, "límite de 9000 en Mascotas para enero",
                     limit_amount=9000, limit_category="Mascotas", limit_month=1)
    assert year.event_key == "limit.year_confirmation"
    assert year.event_variables == {"year": 2027}
    category = await click(year)
    assert category.event_key == "category.confirmation_required"
    done = await click(category)
    assert done.event_key == "limit.created"
    with sessions() as session:
        row = session.scalar(select(LimiteCategoria))
        assert (row.inicio_periodo.year, row.inicio_periodo.month) == (2027, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_cancellation_does_not_create_category_or_limit(environment, typed):
    sessions, llm = environment
    pending = await say(llm, "límite 5000 en Mascotas", limit_amount=5000,
                        limit_category="Mascotas")
    if typed:
        await say(llm, "cancelar", intent="reject_limit")
    else:
        await click(pending, 1)
    assert await ConversationService.get_pending_limit(PHONE) is None
    with sessions() as session:
        assert session.scalar(select(LimiteCategoria)) is None
        assert session.scalar(select(Categoria).where(Categoria.nombre == "Mascotas")) is None


@pytest.mark.asyncio
async def test_direct_creation_and_typed_update_emit_distinct_events(environment):
    sessions, llm = environment
    created = await say(llm, "límite 5000 en Comida", limit_amount=5000,
                        limit_category="Comida")
    assert created.event_key == "limit.created"
    changed = await say(llm, "que sea 7000", intent="change_limit", limit_amount=7000)
    assert changed.event_key == "limit.updated"
    assert changed.reply_message.body.startswith("Actualizado:")
    with sessions() as session:
        rows = session.scalars(select(LimiteCategoria)).all()
        assert len(rows) == 1
        assert rows[0].cantidad_max == Decimal("7000")


@pytest.mark.asyncio
async def test_typed_year_and_category_confirmations_keep_event_metadata(environment):
    _, llm = environment
    await say(llm, "límite 5000 Mascotas enero", limit_amount=5000,
              limit_category="Mascotas", limit_month=1)
    category = await say(llm, "sí", intent="confirm_limit")
    assert category.event_key == "category.confirmation_required"
    created = await say(llm, "sí, creala", intent="confirm_category")
    assert created.event_key == "limit.created"
    assert created.reply_message.body.startswith("Creado:")


@pytest.mark.asyncio
async def test_failed_persistence_never_renders_success(environment, monkeypatch):
    sessions, llm = environment
    monkeypatch.setattr(limit.LimitService, "create_limit", lambda *a, **kw:
                        LimitResult(status="persistence_error", message="failed"))
    failed = await say(llm, "límite 5000 Comida", limit_amount=5000,
                       limit_category="Comida")
    assert failed.event_key is None
    assert failed.reply_message is None
    assert "problema guardando" in failed.reply_text
    with sessions() as session:
        assert session.scalar(select(LimiteCategoria)) is None


@pytest.mark.asyncio
async def test_llm_failure_keeps_pending_data_and_does_not_save(environment):
    sessions, llm = environment
    await say(llm, "quiero un límite de Comida", limit_category="Comida")
    failed = await say(llm, "5000", intent="out_of_scope", error="provider unavailable")
    assert failed.event_key is None
    assert (await ConversationService.get_pending_limit(PHONE)).amount is None
    with sessions() as session:
        assert session.scalar(select(LimiteCategoria)) is None


@pytest.mark.asyncio
async def test_budget_warning_survives_configured_success(environment, monkeypatch):
    _, llm = environment
    monkeypatch.setattr(budget.BudgetService, "get_status_for_limit", lambda *_:
                        SimpleNamespace(status="ok", budget=SimpleNamespace(
                            state="exceeded", category_name="Comida",
                            period_start=datetime(2026, 9, 1), spent_amount=6000,
                            limit_amount=5000, remaining_amount=0, exceeded_amount=1000,
                            currency="ARS", percentage=120,
                        )))
    result = await say(llm, "límite 5000 Comida", limit_amount=5000,
                       limit_category="Comida")
    assert result.reply_message.body.startswith("Creado:")
    assert len(result.followup_messages) == 1
    assert "Superaste el límite" in result.followup_messages[0].body


def test_start_limit_is_not_allowed_during_pending_confirmation():
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition("category.confirmation_required", definition(
            "No debe reiniciar una confirmación pendiente", [("start_limit", "Crear límite")],
        ))


def test_journey_keeps_event_permissions_and_rejects_skipping_confirmation():
    journey = limit_journey_definition()
    node = next(n for n in journey['nodes'] if n['id'] == 'started')
    node['options'][0]['action'] = 'confirm_limit_category'
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)
    node['options'][0].pop('action')
    node['options'][0]['next_node'] = 'created'
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)


def test_journey_rejects_missing_stage_and_wrong_variables():
    journey = limit_journey_definition()
    journey['event_nodes'].pop('limit.created')
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)
    journey = limit_journey_definition()
    journey['nodes'][1]['body'] = '{amount}'  # Amount is unavailable at the start.
    with pytest.raises(ConversationFlowDefinitionInvalid):
        validate_flow_definition(LIMIT_JOURNEY, journey)


def test_journey_publishes_all_messages_and_examples_together(environment):
    from app.services.conversation_flow import ConversationFlowConflict
    flow = next(f for f in ConversationFlowService.list() if f.event_key == LIMIT_JOURNEY)
    journey = flow.published.definition
    journey['response_examples']['datos'] = 'Transporte, 50000, noviembre'
    journey['nodes'][1]['body'] = 'Indicá categoría, monto y mes juntos.'
    ConversationFlowService.save_draft(flow.id, definition=journey)
    assert ConversationFlowService.get(flow.id).published.definition['nodes'][1]['body'] != journey['nodes'][1]['body']
    published = ConversationFlowService.publish(flow.id)
    assert published.published.definition['response_examples']['datos'] == 'Transporte, 50000, noviembre'
    for event in journey['event_nodes']:
        assert ConversationFlowService.find_published_by_event(event).id == flow.id
    competing = ConversationFlowService.create(
        slug='competidor', name='Competidor', event_key='limit.created',
        definition=definition('Guardado'),
    )
    with pytest.raises(ConversationFlowConflict):
        ConversationFlowService.publish(competing.id)


@pytest.mark.asyncio
async def test_button_then_one_message_with_all_fields(environment):
    sessions, llm = environment
    listing = await say(llm, 'ver mis límites', intent='list_limits')
    started = await click(listing)
    assert started.event_key == 'limit.started'
    created = await say(llm, 'Comida, 80000, octubre', limit_category='Comida', limit_amount=80000, limit_month=10)
    assert created.event_key == 'limit.created'
    assert 'Octubre' in created.reply_message.body
    with sessions() as session:
        entry = session.scalar(select(LimiteCategoria))
        assert entry.inicio_periodo.month == 10


@pytest.mark.asyncio
async def test_empty_text_request_asks_for_all_fields(environment):
    _, llm = environment
    result = await say(llm, 'quiero crear un límite')
    assert result.event_key == 'limit.started'
    assert 'Indicá categoría, monto y mes' in result.reply_message.body


@pytest.mark.asyncio
@pytest.mark.parametrize('typed', [False, True])
async def test_shared_category_subflow_returns_to_movement(environment, typed):
    from app.models.database import MovimientoFinanciero
    sessions, llm = environment
    llm.return_value = {'intent': 'expense', 'movement_type': 'egreso', 'amount': 2500,
                        'currency': 'ARS', 'description': 'semillas', 'category': 'Jardinería'}
    pending = await process_incoming_message(PHONE, 'Gasté 2500 en semillas', whatsapp_message_id='shared-category-movement')
    assert pending.event_key == 'category.confirmation_required'
    assert '¿Querés crearla?' in pending.reply_message.body
    with sessions() as session:
        assert session.scalar(select(MovimientoFinanciero)) is None
        assert session.scalar(select(Categoria).where(Categoria.nombre == 'Jardinería')) is None
    if typed:
        done = await say(llm, 'sí', intent='confirm_category')
    else:
        done = await click(pending)
    assert done.event_key == 'movement.registered'
    with sessions() as session:
        category = session.scalar(select(Categoria).where(Categoria.nombre == 'Jardinería'))
        movement = session.scalar(select(MovimientoFinanciero))
        assert movement.categoria_id == category.id
        assert session.scalar(select(LimiteCategoria)) is None
    await click(pending)  # Consumed interaction cannot create a second movement.
    with sessions() as session:
        assert len(session.scalars(select(MovimientoFinanciero)).all()) == 1


@pytest.mark.asyncio
async def test_category_failure_retains_caller_and_retry_returns_to_limit(environment, monkeypatch):
    from app.services.category_creation import CategoryCreationService
    from app.services.finance import CategoryResult
    sessions, llm = environment
    pending = await say(llm, 'Mascotas 5000 octubre', limit_amount=5000, limit_category='Mascotas', limit_month=10)
    real_confirm = CategoryCreationService.confirm
    monkeypatch.setattr(CategoryCreationService, 'confirm', lambda *args: CategoryResult('error', 'failed'))
    failed = await click(pending)
    assert failed.event_key is None
    assert 'No pude crear' in failed.reply_text
    assert await ConversationService.get_pending_limit(PHONE) is not None
    with sessions() as session:
        assert session.scalar(select(LimiteCategoria)) is None
    monkeypatch.setattr(CategoryCreationService, 'confirm', real_confirm)
    done = await say(llm, 'sí', intent='confirm_category')
    assert done.event_key == 'limit.created'


@pytest.mark.asyncio
async def test_parent_failure_does_not_repeat_category_creation(environment, monkeypatch):
    sessions, llm = environment
    pending = await say(llm, 'Mascotas 5000 octubre', limit_amount=5000, limit_category='Mascotas', limit_month=10)
    real_create = limit.LimitService.create_limit
    monkeypatch.setattr(limit.LimitService, 'create_limit', lambda *args, **kwargs: LimitResult('persistence_error', 'failed'))
    failed = await click(pending)
    assert failed.event_key is None
    assert 'La categoría ya está disponible' in failed.reply_text
    with sessions() as session:
        assert len(session.scalars(select(Categoria).where(Categoria.nombre == 'Mascotas')).all()) == 1
        assert session.scalar(select(LimiteCategoria)) is None
    monkeypatch.setattr(limit.LimitService, 'create_limit', real_create)
    done = await say(llm, 'sí', intent='confirm_category')
    assert done.event_key == 'limit.created'
    with sessions() as session:
        assert len(session.scalars(select(Categoria).where(Categoria.nombre == 'Mascotas')).all()) == 1
        assert len(session.scalars(select(LimiteCategoria)).all()) == 1


@pytest.mark.asyncio
async def test_one_publication_changes_category_prompt_for_both_callers(environment):
    _, llm = environment
    flow = ConversationFlowService.find_published_by_event('category.confirmation_required')
    payload = flow.published.definition
    payload['nodes'][0]['body'] = 'Compartido: ¿creamos {category}?'
    ConversationFlowService.save_draft(flow.id, definition=payload)
    ConversationFlowService.publish(flow.id)
    pending_limit = await say(llm, 'Mascotas 5000', limit_amount=5000, limit_category='Mascotas')
    assert pending_limit.reply_message.body == 'Compartido: ¿creamos Mascotas?'
    await click(pending_limit, 1)
    llm.return_value = {'intent': 'expense', 'movement_type': 'egreso', 'amount': 2500,
                        'currency': 'ARS', 'description': 'semillas', 'category': 'Jardinería'}
    pending_movement = await process_incoming_message(PHONE, 'Gasté 2500 en semillas', whatsapp_message_id='shared-prompt')
    assert pending_movement.reply_message.body == 'Compartido: ¿creamos Jardinería?'


def test_category_service_can_be_used_without_a_parent_operation(environment):
    from app.models.database import MovimientoFinanciero
    from app.services.category_creation import CategoryCreationService
    sessions, _ = environment
    created = CategoryCreationService.confirm(PHONE, 'Mascotas')
    existing = CategoryCreationService.confirm(PHONE, 'Mascotas')
    assert (created.status, existing.status) == ('created', 'already_exists')
    assert created.category_id == existing.category_id
    with sessions() as session:
        assert session.scalar(select(MovimientoFinanciero)) is None
        assert session.scalar(select(LimiteCategoria)) is None
