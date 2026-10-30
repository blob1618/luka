"""Transport: un solo emisor por enlace de dashboard (tool vs flujo configurado)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.whatsapp import WhatsAppCTAURL, WhatsAppText
from app.services.assistant_transport import assemble_reply
from app.services.conversation_flow_runtime import ConversationFlowRuntime

URL = 'https://app.example/login?token=t'


def answer(**overrides):
    base = dict(text='', outbound=[], event_key=None, event_variables={}, action_binding=None,
                confirmation_action=None, committed=None, replayed=False, preview_delivery=None,
                trace=[], clear_memory=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def ctas(result):
    return [m for m in [result.reply_message, *result.followup_messages] if isinstance(m, WhatsAppCTAURL)]


def flow_returning(message):
    return AsyncMock(return_value=message)


@pytest.mark.asyncio
async def test_dashboard_link_is_sent_once_with_configured_flow(monkeypatch):
    tool_cta = WhatsAppCTAURL('Tu acceso personal al dashboard.', 'Abrir dashboard', URL)
    configured = WhatsAppCTAURL('Accedé al panel desde el botón.', 'Abrir panel', URL)
    monkeypatch.setattr(ConversationFlowRuntime, 'render_event', flow_returning(configured))

    result = await assemble_reply('5411', answer(text='Tu acceso personal al dashboard.',
        outbound=[tool_cta], event_key='dashboard.link.sent',
        event_variables={'login_url': URL, 'ttl_minutes': '10'}))

    found = ctas(result)
    assert len(found) == 1 and found[0] is configured
    assert isinstance(result.reply_message, WhatsAppCTAURL) and result.reply_text == configured.body
    assert result.followup_messages == []


@pytest.mark.asyncio
async def test_dashboard_link_falls_back_to_tool_message_without_flow(monkeypatch):
    tool_cta = WhatsAppCTAURL('Tu acceso personal al dashboard.', 'Abrir dashboard', URL)
    monkeypatch.setattr(ConversationFlowRuntime, 'render_event', flow_returning(None))

    result = await assemble_reply('5411', answer(text='Tu acceso personal al dashboard.',
        outbound=[tool_cta], event_key='dashboard.link.sent',
        event_variables={'login_url': URL, 'ttl_minutes': '10'}))

    found = ctas(result)
    assert len(found) == 1 and found[0] is tool_cta
    assert result.followup_messages == []


@pytest.mark.asyncio
async def test_movement_list_keeps_text_and_sends_one_link(monkeypatch):
    tool_cta = WhatsAppCTAURL('Podés ver los demás movimientos en tu dashboard.', 'Abrir dashboard', URL)
    configured = WhatsAppCTAURL('Accedé al panel para ver el resto.', 'Abrir panel', URL)
    monkeypatch.setattr(ConversationFlowRuntime, 'render_event', flow_returning(configured))

    result = await assemble_reply('5411', answer(text='• Movimiento 1: ARS 100,00.',
        outbound=[tool_cta], event_key='movements.query_result',
        event_variables={'summary': 'x', 'dashboard_url': URL, 'ttl_minutes': '10'}))

    assert isinstance(result.reply_message, WhatsAppText) and result.reply_text.startswith('• Movimiento')
    found = ctas(result)
    assert len(found) == 1 and found[0] is configured


@pytest.mark.asyncio
async def test_other_events_still_append_configured_message(monkeypatch):
    configured = WhatsAppText('Elegí un límite para continuar.')
    monkeypatch.setattr(ConversationFlowRuntime, 'render_event', flow_returning(configured))

    result = await assemble_reply('5411', answer(text='Límite creado.',
        event_key='limit.listed', event_variables={'summary': 'x'}))

    assert result.reply_text == 'Límite creado.'
    assert result.followup_messages == [configured]
