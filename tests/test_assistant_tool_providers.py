"""Native wire contracts for all three adapters, using mocked HTTP only."""
import json

import pytest

from app.services.llm_providers.deepseek import DeepSeekProvider
from app.services.llm_providers.gemini import GeminiProvider
from app.services.llm_providers.mistral import MistralProvider
from app.services.llm_providers.tools import GenerationPolicy, ToolDefinition, ToolMessage


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_cls', [GeminiProvider, MistralProvider, DeepSeekProvider])
async def test_native_roundtrip_preserves_parts_and_call_ids(provider_cls, monkeypatch):
    provider = provider_cls()
    monkeypatch.setattr(provider, '_get_config', lambda: ('synthetic-not-a-key', 'test-model'))
    sent = []
    gemini = provider_cls is GeminiProvider
    native_message = ({'role': 'model', 'parts': [{'functionCall': {'id': 'call-native', 'name': 'wallet_balances', 'args': {}},
                        'thoughtSignature': 'opaque-synthetic-signature'}]} if gemini else
                      {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-native', 'type': 'function',
                          'function': {'name': 'wallet_balances', 'arguments': '{}'}}]})
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'candidates': [{'content': native_message}]} if gemini else {'choices': [{'message': native_message}]}
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            sent.append(kwargs['json'])
            return Response()
    monkeypatch.setattr('app.services.llm_providers.tools.httpx.AsyncClient', Client)
    policy = GenerationPolicy('owner:turn', verified_models=('test-model',))
    definitions = [ToolDefinition('wallet_balances', 'Own balances', {'type': 'object', 'additionalProperties': False})]
    first = await provider.generate_with_tools(messages=[ToolMessage('system', 'rules'), ToolMessage('user', 'balances')],
        tools=definitions, continuation=None, policy=policy)
    assert first.calls[0].id == 'call-native'
    await provider.generate_with_tools(messages=[ToolMessage('system', 'rules'),
        ToolMessage('tool', json.dumps({'status': 'ok'}), 'call-native', 'wallet_balances')],
        tools=definitions, continuation=first.continuation, policy=policy)
    if gemini:
        assert sent[1]['contents'][1] == native_message
        assert sent[1]['contents'][2]['parts'][0]['functionResponse']['id'] == 'call-native'
        assert 'parametersJsonSchema' in sent[0]['tools'][0]['functionDeclarations'][0]
        assert 'responseMimeType' not in sent[0]['generationConfig']
    else:
        assert sent[1]['messages'][-1]['tool_call_id'] == 'call-native'
        assert sent[1]['messages'][-2] == native_message
        assert 'response_format' not in sent[0]
        if provider_cls is DeepSeekProvider:
            assert sent[0]['thinking'] == {'type': 'disabled'}
    with pytest.raises(ValueError, match='Continuation'):
        await provider.generate_with_tools(messages=[], tools=definitions, continuation=first.continuation,
            policy=GenerationPolicy('other:turn', verified_models=('test-model',)))


@pytest.mark.asyncio
async def test_unverified_model_does_not_make_a_request(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setattr(provider, '_get_config', lambda: ('synthetic', 'unverified-model'))
    with pytest.raises(ValueError, match='not enabled'):
        await provider.generate_with_tools(messages=[], tools=[], policy=GenerationPolicy('scope'))


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', ['multiple', 'malformed', 'http_error'])
async def test_multiple_calls_and_transport_errors_are_not_reinterpreted(monkeypatch, wire):
    import httpx
    provider = DeepSeekProvider()
    monkeypatch.setattr(provider, '_get_config', lambda: ('synthetic', 'test-model'))
    count = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            count.append(url)
            request = httpx.Request('POST', url)
            if wire == 'http_error':
                return httpx.Response(503, request=request)
            args = '[]' if wire == 'malformed' else '{}'
            return httpx.Response(200, request=request, json={'choices': [{'message': {
                'role': 'assistant', 'tool_calls': [
                    {'id': 'one', 'function': {'name': 'wallet_balances', 'arguments': args}},
                    {'id': 'two', 'function': {'name': 'find_movements', 'arguments': '{}'}},
                ]}}]})
    monkeypatch.setattr('app.services.llm_providers.tools.httpx.AsyncClient', Client)
    kwargs = dict(messages=[ToolMessage('user', 'consulta')], tools=[], policy=GenerationPolicy('turn', verified_models=('test-model',)))
    if wire == 'multiple':
        result = await provider.generate_with_tools(**kwargs)
        assert [c.id for c in result.calls] == ['one', 'two']
    else:
        with pytest.raises(httpx.HTTPStatusError if wire == 'http_error' else ValueError):
            await provider.generate_with_tools(**kwargs)
    assert len(count) == 1  # no unverified-model fallback or application execution
