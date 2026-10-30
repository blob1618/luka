"""Native tool protocol. Transport never executes application capabilities."""
from dataclasses import dataclass, field
import json
from typing import Any, Literal

import httpx


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolMessage:
    role: Literal['system', 'user', 'tool']
    content: str
    call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class GenerationPolicy:
    scope: str
    timeout: float = 30.0
    verified_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderContinuation:
    provider: str
    model: str
    scope: str
    history: list[dict[str, Any]] = field(repr=False)


@dataclass(frozen=True)
class ToolTurn:
    calls: list[ToolCall]
    continuation: ProviderContinuation
    usage: dict[str, Any] = field(default_factory=dict)


def _call(call_id, name, arguments):
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or not isinstance(arguments, dict):
        raise ValueError('Malformed native function call')
    return ToolCall(call_id, name, arguments)


async def native_tool_turn(provider, *, messages, tools, continuation, policy):
    """One request to the configured model, without model fallback or tool retries."""
    model = provider._get_model()
    if model not in policy.verified_models:
        raise ValueError('The configured model is not enabled for native tools')
    name = provider.LOG_PREFIX.lower()
    if continuation and (continuation.provider, continuation.model, continuation.scope) != (name, model, policy.scope):
        raise ValueError('Continuation belongs to another provider, model or turn')
    history = list(continuation.history) if continuation else []
    api_key, _ = provider._get_config()
    if name == 'gemini':
        system = []
        for message in messages:
            if message.role == 'system':
                system.append({'text': message.content})
            elif message.role == 'tool':
                history.append({'role': 'user', 'parts': [{'functionResponse': {
                    'id': message.call_id, 'name': message.name,
                    'response': json.loads(message.content),
                }}]})
            else:
                history.append({'role': 'user', 'parts': [{'text': message.content}]})
        # System instructions are supplied on every step by the runtime.
        body = {'contents': list(history), 'systemInstruction': {'parts': system},
                'tools': [{'functionDeclarations': [
                    {'name': t.name, 'description': t.description, 'parametersJsonSchema': t.parameters} for t in tools]}],
                'generationConfig': {'temperature': 0.1}}
        headers = {'x-goog-api-key': api_key}
        url = provider.API_URL.format(model=model)
    else:
        for message in messages:
            item = {'role': message.role, 'content': message.content}
            if message.role == 'tool':
                item['tool_call_id'] = message.call_id
            elif continuation and message.role == 'system':
                continue
            history.append(item)
        body = {'model': model, 'messages': list(history), 'temperature': 0.1,
                'tools': [{'type': 'function', 'function': {
                    'name': t.name, 'description': t.description, 'parameters': t.parameters}} for t in tools],
                'tool_choice': 'auto'}
        if name == 'deepseek':
            body['thinking'] = {'type': 'disabled'}
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        url = provider.API_URL
    async with httpx.AsyncClient(timeout=max(0.1, min(policy.timeout, 30.0))) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
    payload = response.json()
    if name == 'gemini':
        content = payload['candidates'][0]['content']
        calls = [_call(part['functionCall'].get('id') or f'gemini-{len(history)}-{index}',
                       part['functionCall'].get('name'), part['functionCall'].get('args'))
                 for index, part in enumerate(content.get('parts', [])) if 'functionCall' in part]
        # Keep all native parts, including opaque thought signatures, untouched.
        history.append(content)
        usage = payload.get('usageMetadata', {})
    else:
        content = payload['choices'][0]['message']
        calls = [_call(c.get('id'), c.get('function', {}).get('name'), c.get('function', {}).get('arguments'))
                 for c in content.get('tool_calls', [])]
        history.append(content)
        usage = payload.get('usage', {})
    if len({c.id for c in calls}) != len(calls):
        raise ValueError('Duplicate native tool call IDs')
    # Free-form financial text is deliberately not returned to the runtime.
    return ToolTurn(calls, ProviderContinuation(name, model, policy.scope, history), usage)
