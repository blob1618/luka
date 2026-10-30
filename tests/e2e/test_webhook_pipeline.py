"""Pipeline completo por HTTP: webhook firmado, dispatcher real y Meta simulado."""
import httpx

from app.models.database import AssistantWriteReceipt, MovimientoFinanciero
from app.services.dashboard_link import (
    DashboardLinkDecision,
    DashboardLinkResult,
    DashboardLinkService,
)
from tests.e2e.support import (
    button_payload,
    confirmation_button_id,
    outbound_messages,
    text_payload,
)

REGISTER = {
    'economic_effect': 'cash_flow',
    'movement_type': 'egreso',
    'amount': '5000',
    'currency': 'ARS',
    'description': 'Nafta',
    'date_spec': {'kind': 'absent'},
}


def confirmation_buttons(graph):
    return [button
            for payload in outbound_messages(graph)
            for button in payload.get('interactive', {}).get('action', {}).get('buttons', [])
            if button.get('reply', {}).get('id', '').startswith('assistant:confirm:')]


def test_text_message_sends_preview_buttons_without_writing(webhook, user, graph, script, isolated_db):
    script([[('register_movement', REGISTER)]])

    response = webhook(text_payload())

    assert response.status_code == 200
    assert confirmation_buttons(graph), graph
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0


def test_button_reply_commits_movement_and_receipt(webhook, user, graph, script, isolated_db):
    script([[('register_movement', REGISTER)]])
    webhook(text_payload())
    button_id = confirmation_button_id(graph)

    response = webhook(button_payload(button_id))

    assert response.status_code == 200
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 1
        assert session.query(AssistantWriteReceipt).count() == 1
    bodies = [payload.get('text', {}).get('body', '')
              for payload in outbound_messages(graph) if payload.get('type') == 'text']
    assert any('Registré' in body for body in bodies), bodies


def test_duplicate_wamid_is_processed_once(webhook, user, graph, script):
    script([[('register_movement', REGISTER)]])
    payload = text_payload()

    assert webhook(payload).status_code == 200
    assert webhook(payload).status_code == 200

    assert len(confirmation_buttons(graph)) == 1, graph


def test_graph_failure_keeps_webhook_healthy(webhook, user, script, isolated_db, monkeypatch):
    def handler(request):
        return httpx.Response(500, json={'error': {'message': 'boom'}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr('app.api.whatsapp.get_whatsapp_client', lambda: client)
    script([[('register_movement', REGISTER)]])

    response = webhook(text_payload())

    assert response.status_code == 200
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 0


def test_dashboard_sends_a_single_link(webhook, user, graph, script, monkeypatch):
    result = DashboardLinkResult(DashboardLinkDecision.SEND_LINK,
                                 login_url='https://app.example/login?token=e2e',
                                 link_ttl_minutes=10)
    monkeypatch.setattr(DashboardLinkService, 'generate_or_reuse',
                        classmethod(lambda cls, *args, **kwargs: result))
    script([[('dashboard_link', {})]])

    response = webhook(text_payload(body='Mandame el dashboard'))

    assert response.status_code == 200
    ctas = [payload for payload in outbound_messages(graph)
            if payload.get('interactive', {}).get('action', {}).get('name') == 'cta_url']
    assert len(ctas) == 1, graph
