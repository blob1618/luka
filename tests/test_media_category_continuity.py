"""Synthetic conversations: real operations/DB, scripted tools, credited delivery."""
# ruff: noqa: F811
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.models.database import Categoria, MovimientoFinanciero
from app.services import assistant_state
from app.services.assistant_actions import _bound_action
from app.services.assistant_runtime import make_context, record_preview_delivered, run_for_media, run_for_phone
from app.services.assistant_questions import record_question_delivered
from app.services.media_contracts import InboundMedia, MediaExtraction, ValidatedMedia
from tests.test_assistant_pilot import accounts, PHONE, NativeScript, amend_active  # noqa: F401


async def attach(identifier='pdf', *, kind='document', date='2 Oct, 2025', category='Juegos', description='Juego ficticio'):
    mime = 'application/pdf' if kind == 'document' else 'image/jpeg'
    async def load(_):
        return ValidatedMedia(kind, mime, b'fixture')
    async def extract(_):
        return MediaExtraction(transcript=f'{description} {date or ""}', amount='1.44', currency='USD',
            description=description, category=category, date_text=date, payment_status='paid',
            document_type='invoice' if kind == 'document' else None)
    return await run_for_media(PHONE, identifier, InboundMedia(identifier, PHONE, 'fixture', kind, mime),
                               load_media=load, extract=extract)


async def delivered(answer):
    if answer.preview_delivery:
        await record_preview_delivered(PHONE, answer.preview_delivery)
    if answer.question_delivery:
        await record_question_delivered(PHONE, answer.question_delivery)


def answer_new(kwargs):
    initial = json.loads(next(m.content for m in kwargs['messages'] if m.role == 'user'))['context']
    q = initial['pending_question']
    assert q['field'] == 'category_ref'
    return [('answer_category_question', {'operation_ref': q['operation_ref'],
        'expected_revision': q['revision'], 'new_name': 'Juegos'})]


def count(accounts):
    with accounts[0]() as db:
        return db.query(MovimientoFinanciero).filter_by(descripcion='Juego ficticio').count()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['text', 'button', 'editor'])
@pytest.mark.parametrize('date', ['2 Oct, 2025', '04/11/2026'])
async def test_child_continues_parent_and_requires_separate_delivered_confirmation(accounts, entry, date):
    first = await attach(date=date)
    assert first.question_delivery.field == 'category_ref'
    await delivered(first)
    child = await run_for_phone(PHONE, 'new-category', 'Juegos', provider=NativeScript([answer_new]))
    await delivered(child)
    if entry == 'editor':
        state = await assistant_state.read(accounts[1])
        context = make_context(accounts[1], PHONE, 'confirm-child', '', None, state)
        result = await _bound_action(context, 'confirm_category', {}, child.action_binding)
        await assistant_state.save(accounts[1], state)
    else:
        result = await run_for_phone(PHONE, 'confirm-child', 'sí' if entry == 'text' else '',
                                    action=child.confirmation_action if entry == 'button' else None)
    assert 'creada' in result.text
    assert count(accounts) == 0
    with accounts[0]() as db:
        assert db.query(Categoria).filter_by(nombre='Juegos', usuario_id=accounts[1]).count() == 1
    if date == '2 Oct, 2025':
        assert 'fecha' in result.text
        assert result.question_delivery.field == 'date_spec'
        await delivered(result)
        result = await run_for_phone(PHONE, 'date', 'el 04/11/2026', provider=NativeScript([
            amend_active({'date_spec': {'kind': 'literal', 'evidence_span': '04/11/2026'}})]))
    assert result.preview_delivery
    denied = await run_for_phone(PHONE, 'undelivered-confirm', 'sí')
    assert 'entregada' in denied.text
    assert count(accounts) == 0
    await delivered(result)
    confirmed = await run_for_phone(PHONE, 'confirm-movement', action=result.confirmation_action)
    assert confirmed.committed
    assert count(accounts) == 1
    replay = await run_for_phone(PHONE, 'replay', action=result.confirmation_action)
    assert replay.replayed and count(accounts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['document', 'image'])
@pytest.mark.parametrize('choice', ['  cOMida  ', '1'])
async def test_existing_choice_never_invokes_model_or_previous_search(accounts, kind, choice):
    await run_for_phone(PHONE, 'search', 'gastos', provider=NativeScript([
        [('find_movements', {'selector': {'kind': 'all'}})], [('present_result', {})]]))
    first = await attach(kind=kind, date='04/11/2026')
    await delivered(first)
    provider = NativeScript([])
    result = await run_for_phone(PHONE, 'category', choice, provider=provider)
    assert provider.calls == 0
    assert result.preview_delivery and 'Comida' in result.text
    assert count(accounts) == 0


@pytest.mark.asyncio
async def test_undelivered_question_cannot_bind_child(accounts):
    first = await attach()
    q = first.question_delivery
    result = await run_for_phone(PHONE, 'answer', 'Juegos', provider=NativeScript([
        [('answer_category_question', {'operation_ref': q.operation_ref, 'expected_revision': q.revision, 'new_name': 'Juegos'})], []]))
    assert 'no fue entregada' in result.text
    state = await assistant_state.read(accounts[1])
    assert len(state['workspace']['operations']) == 1


@pytest.mark.asyncio
async def test_two_attachments_old_delivery_and_revision_rejected(accounts):
    first = await attach('one', description='Primero')
    await delivered(first)
    second = await attach('two', description='Segundo')
    with pytest.raises(ValueError):
        await delivered(first)
    await delivered(second)
    q = first.question_delivery
    result = await run_for_phone(PHONE, 'old-answer', 'Juegos', provider=NativeScript([
        [('answer_category_question', {'operation_ref': q.operation_ref, 'expected_revision': q.revision, 'new_name': 'Juegos'})], []]))
    assert 'pregunta de categoría cambió' in result.text
    state = await assistant_state.read(accounts[1])
    context = make_context(accounts[1], PHONE, 'inspect', '', None, state)
    projection = await context.initial_context()
    assert len(projection['operations']) == 2
    assert any('Primero' in op['summary'] for op in projection['operations'])
    assert any('Segundo' in op['summary'] for op in projection['operations'])
    assert projection['pending_question']['operation_ref'] == second.question_delivery.operation_ref


@pytest.mark.asyncio
async def test_interruption_expiry_and_foreign_owner_cannot_use_question(accounts):
    first = await attach()
    await delivered(first)
    with pytest.raises(ValueError):
        await record_question_delivered('5491100260002', first.question_delivery)
    await run_for_phone(PHONE, 'read', 'gastos', provider=NativeScript([
        [('find_movements', {'selector': {'kind': 'all'}})], [('present_result', {})]]))
    state = await assistant_state.read(accounts[1])
    assert 'pending_question' not in state
    first = await attach('new')
    with pytest.raises(ValueError):
        await record_question_delivered(PHONE, first.question_delivery, now=datetime.now(timezone.utc) + timedelta(hours=1))


@pytest.mark.asyncio
async def test_active_resume_is_recoverable_and_status_does_not_mutate(accounts):
    first = await attach()
    await delivered(first)
    state = await assistant_state.read(accounts[1])
    result = await run_for_phone(PHONE, 'status', '¿Registraste el gasto?', provider=NativeScript([
        [('operation_status', {})]]))
    assert 'pendiente' in result.text and count(accounts) == 0
    assert (await assistant_state.read(accounts[1]))['workspace'] == state['workspace']
    q = first.question_delivery
    result = await run_for_phone(PHONE, 'resume', 'retomalo', provider=NativeScript([
        [('operation_resume', {'ref': q.operation_ref, 'expected_revision': q.revision})], []]))
    assert 'ya está activa' not in result.text
    assert result.question_delivery.revision > q.revision
    assert 'categoría' in result.text


@pytest.mark.asyncio
async def test_photo_without_date_requests_it_instead_of_receipt_day(accounts):
    result = await attach(kind='image', date=None, category='Comida')
    assert result.preview_delivery is None
    assert result.question_delivery.field == 'date_spec'
    assert count(accounts) == 0


@pytest.mark.asyncio
async def test_question_delivery_wrapper_failure_retry_and_replay(accounts, fake_redis):
    from app.models.database import Usuario
    from app.services.assistant_transport import assemble_reply
    from app.services.webhook_idempotency import process_text_message_once
    from uuid import uuid4
    with accounts[0]() as db:
        db.get(Usuario, accounts[1]).auth_user_id = uuid4()
        db.commit()
    first = await attach()
    result = await assemble_reply(PHONE, first)
    async def process(**kwargs):
        return result
    async def fail(*args):
        return False
    async def send(*args):
        return True
    kwargs = dict(redis_client=fake_redis, whatsapp_message_id='question-send', sender_phone=PHONE,
                  text_body='fixture', process_message=process)
    with pytest.raises(RuntimeError):
        await process_text_message_once(**kwargs, send_message=fail)
    state = await assistant_state.read(accounts[1])
    assert not state['pending_question']['delivered']
    await process_text_message_once(**kwargs, send_message=send)
    assert (await assistant_state.read(accounts[1]))['pending_question']['delivered']
    await delivered(first)  # repeated callback is harmless
    assert await process_text_message_once(**kwargs, send_message=send) == 'duplicate'


@pytest.mark.asyncio
async def test_independent_category_does_not_complete_parent(accounts):
    first = await attach()
    await delivered(first)
    child = await run_for_phone(PHONE, 'independent', 'Creá otra categoría aparte: Mascotas',
        provider=NativeScript([[('create_category', {'name': 'Mascotas'})]]))
    await delivered(child)
    result = await run_for_phone(PHONE, 'independent-confirm', action=child.confirmation_action)
    assert 'creada' in result.text and not result.preview_delivery
    state = await assistant_state.read(accounts[1])
    parent = next(op for op in state['workspace']['operations'].values() if op['operation_key'] == 'register_movement')
    assert parent['status'] == 'suspended' and parent['draft_payload']['category_ref'] is None


@pytest.mark.asyncio
async def test_changed_parent_blocks_child_confirmation(accounts):
    first = await attach()
    await delivered(first)
    child = await run_for_phone(PHONE, 'child', 'Juegos', provider=NativeScript([answer_new]))
    await delivered(child)
    state = await assistant_state.read(accounts[1])
    context = make_context(accounts[1], PHONE, 'cancel-parent', '', None, state)
    parent = next(op for op in context.operations.workspace(context).operations.values()
                  if op.operation_key == 'register_movement')
    view = await context.operations.view(context, parent)
    await context.operations.cancel(context, view.ref, expected_revision=parent.revision)
    await assistant_state.save(accounts[1], state)
    result = await run_for_phone(PHONE, 'confirm-stale', action=child.confirmation_action)
    assert not result.committed and count(accounts) == 0
    with accounts[0]() as db:
        assert db.query(Categoria).filter_by(nombre='Juegos').count() == 0


@pytest.mark.asyncio
async def test_model_cannot_remove_media_category_requirement(accounts):
    first = await attach(date='04/11/2026')
    await delivered(first)
    result = await run_for_phone(PHONE, 'amend-requirement', 'continuá', provider=NativeScript([
        amend_active({'category_required': False, 'requested_category': None})]))
    assert result.preview_delivery is None
    assert result.question_delivery.field == 'category_ref'
    assert count(accounts) == 0
