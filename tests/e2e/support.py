"""Helpers compartidos por el arnés E2E."""
from app.services.llm_providers.tools import ProviderContinuation, ToolCall, ToolTurn

PHONE = '5491100260001'
SECRET = 'e2e-app-secret'


class ScriptedProvider:
    """Proveedor determinista: cada turno devuelve las herramientas indicadas."""

    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = 0

    async def generate_with_tools(self, **kwargs):
        self.calls += 1
        step = next(self.steps)
        if callable(step):
            step = step(kwargs)
        return ToolTurn(
            [ToolCall(f'e2e-{self.calls}-{index}', name, args)
             for index, (name, args) in enumerate(step)],
            ProviderContinuation('scripted', 'e2e', kwargs['policy'].scope, []),
        )


def text_payload(message_id='wamid.e2e.1', body='Gasté 5000 en nafta', phone=PHONE):
    return {
        'object': 'whatsapp_business_account',
        'entry': [{'id': 'waba', 'changes': [{'field': 'messages', 'value': {
            'messaging_product': 'whatsapp',
            'metadata': {'phone_number_id': '1234567890'},
            'messages': [{'id': message_id, 'from': phone, 'timestamp': '1760000000',
                          'type': 'text', 'text': {'body': body}}],
        }}]}],
    }


def button_payload(button_id, message_id='wamid.e2e.confirm', phone=PHONE):
    return {
        'object': 'whatsapp_business_account',
        'entry': [{'id': 'waba', 'changes': [{'field': 'messages', 'value': {
            'messaging_product': 'whatsapp',
            'metadata': {'phone_number_id': '1234567890'},
            'messages': [{'id': message_id, 'from': phone, 'timestamp': '1760000001',
                          'type': 'interactive', 'interactive': {
                              'type': 'button_reply',
                              'button_reply': {'id': button_id, 'title': 'Confirmar'}}}],
        }}]}],
    }


def outbound_messages(graph):
    return [payload for payload in graph if payload.get('type') in {'text', 'interactive'}]


def confirmation_button_id(graph):
    for payload in outbound_messages(graph):
        action = payload.get('interactive', {}).get('action', {})
        for button in action.get('buttons', []):
            identifier = button.get('reply', {}).get('id', '')
            if identifier.startswith('assistant:confirm:'):
                return identifier
    raise AssertionError(f'No se envió botón de confirmación: {graph}')
