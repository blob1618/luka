"""Presentation layout adds bullets to list items, never to introductions."""
import json

import pytest

from app.services.assistant_contracts import AskClarification, PresentResult, VerifiedFact
from app.services.assistant_presentation import Presenter
from app.services.assistant_runtime import run_for_phone
from tests import test_assistant_pilot
from tests.test_assistant_pilot import NOW, PHONE, NativeScript

accounts = test_assistant_pilot.accounts

REF = 'ref_' + 'a' * 32


def fact(value, *, list_item=False):
    return VerifiedFact(ref=REF, label='statement', value=value,
                        origin='backend_computed', list_item=list_item)


def test_render_defaults_to_plain():
    presenter = Presenter()
    presenter.add(['Hola'])

    assert presenter.render() == 'Hola'


def test_render_plain_keeps_internal_format():
    value = '*Patrimonio*\n• ARS: $1\n• USD: $2'
    presenter = Presenter()
    presenter.add([value])

    assert presenter.render() == value
    assert presenter.render(layout='plain') == value


def test_render_bullets_only_list_items():
    presenter = Presenter()
    presenter.add(['A continuación te presento tus movimientos:'])
    presenter.add(['Movimiento 1', 'Movimiento 2'], list_item=True)

    assert presenter.render(layout='bullets') == (
        'A continuación te presento tus movimientos:\n'
        '• Movimiento 1\n'
        '• Movimiento 2'
    )


def test_single_list_item_gets_bullet_when_requested():
    presenter = Presenter()
    presenter.add(['Único recordatorio'], list_item=True)

    assert presenter.render(layout='bullets') == '• Único recordatorio'


def test_layout_plain_keeps_list_items_without_bullets():
    presenter = Presenter()
    presenter.add(['Movimiento'], list_item=True)

    assert presenter.render(layout='plain') == 'Movimiento'


def test_include_preserves_list_item_role():
    presenter = Presenter()
    presenter.include([fact('Movimiento', list_item=True)])

    assert presenter.render(layout='bullets') == '• Movimiento'


def test_present_result_schema_exposes_layout_enum():
    schema = PresentResult.model_json_schema()

    assert schema['properties']['layout']['enum'] == ['plain', 'bullets']
    assert schema['properties']['layout']['default'] == 'plain'
    assert 'layout' not in schema.get('required', [])
    assert schema['additionalProperties'] is False


def test_ask_clarification_schema_exposes_layout_enum():
    schema = AskClarification.model_json_schema()

    assert schema['properties']['layout']['enum'] == ['plain', 'bullets']
    assert schema['properties']['layout']['default'] == 'plain'
    assert 'layout' not in schema.get('required', [])


@pytest.mark.asyncio
async def test_two_wallets_render_bullets_when_agent_requests_them(accounts):
    reply = await run_for_phone(PHONE, 'layout-bullets', 'mis saldos', provider=NativeScript([
        [('wallet_balances', {})],
        [('present_result', {'layout': 'bullets'})],
    ]), now=NOW)

    lines = reply.text.splitlines()
    assert len(lines) == 2
    assert all(line.startswith('• ') for line in lines)


@pytest.mark.asyncio
async def test_single_wallet_stays_plain_when_agent_requests_plain(accounts):
    reply = await run_for_phone(PHONE, 'layout-plain', 'mi saldo en pesos', provider=NativeScript([
        [('wallet_balances', {'currency': 'ARS'})],
        [('present_result', {'layout': 'plain'})],
    ]), now=NOW)

    assert '•' not in reply.text
    assert 'Caja A' in reply.text


@pytest.mark.asyncio
async def test_single_wallet_gets_bullet_when_presented_as_list(accounts):
    reply = await run_for_phone(PHONE, 'layout-single', 'mis saldos', provider=NativeScript([
        [('wallet_balances', {'currency': 'ARS'})],
        [('present_result', {'layout': 'bullets'})],
    ]), now=NOW)

    assert reply.text.startswith('• Caja A')


@pytest.mark.asyncio
async def test_movement_summary_stays_plain_under_bullets(accounts):
    reply = await run_for_phone(PHONE, 'layout-summary', 'mis movimientos', provider=NativeScript([
        [('find_movements', {'selector': {'kind': 'all'}, 'sort_by': 'date_desc', 'limit': 2})],
        [('present_result', {'layout': 'bullets'})],
    ]), now=NOW)

    lines = reply.text.splitlines()
    assert len(lines) == 3
    assert all(line.startswith('• ') for line in lines[:2])
    assert lines[2] == 'Mostré 2 de 9 movimientos.'


@pytest.mark.asyncio
async def test_fallback_without_tool_call_stays_plain(accounts):
    reply = await run_for_phone(PHONE, 'layout-fallback', 'pedido raro',
                                provider=NativeScript([[]]), now=NOW)

    assert reply.text == 'Esa capacidad no está disponible o necesito que aclares el pedido. No hice cambios.'
    assert not reply.text.startswith('• ')


@pytest.mark.asyncio
async def test_clarification_bullets_facts_but_keeps_numbered_options(accounts):
    def ask(kwargs):
        data = json.loads(next(message.content for message in kwargs['messages']
                               if message.role == 'tool'))['data']
        return [('ask_clarification', {
            'reason_code': 'ambiguous_reference',
            'target_field': 'movement',
            'candidate_refs': [item['movement_ref'] for item in data['items'][:2]],
            'layout': 'bullets',
        })]

    reply = await run_for_phone(PHONE, 'layout-clarify', 'cuál gasto', provider=NativeScript([
        [('find_movements', {'selector': {'kind': 'all'}, 'sort_by': 'date_desc', 'limit': 2})],
        ask,
    ]), now=NOW)

    bullet_lines = [line for line in reply.text.splitlines() if line.startswith('• ')]
    assert len(bullet_lines) == 2
    assert '1. ' in reply.text and '2. ' in reply.text
