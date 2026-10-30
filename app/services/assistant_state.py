"""Owner-scoped conversation workspace, with a Redis claim held across confirmation."""
from contextlib import asynccontextmanager
import json
import secrets

from app.services.assistant_contracts import ConversationWorkspace, ReferenceRecord
from app.services.conversation import ConversationService, ConversationStateUnavailable
from app.services.telemetry import track_phase

TTL = 1800


def key(owner_id):
    # Stage 5 changes the prepared movement contract. Old ephemeral workspaces
    # are never interpreted with an invented economic effect; receipts stay durable.
    return f'assistant:v3:{owner_id}'


def empty(owner_id, conversation_id='whatsapp'):
    return {'workspace': ConversationWorkspace(owner_id=owner_id, conversation_id=conversation_id).model_dump(mode='json'),
            'references': {}, 'last_shown': []}


async def client():
    try:
        return await ConversationService._get_client()
    except Exception as exc:
        raise ConversationStateUnavailable('No se puede guardar el contexto de conversación') from exc


async def read(owner_id):
    try:
        redis = await client()
        with track_phase('redis'):
            raw = await redis.get(key(owner_id))
        result = json.loads(raw) if raw else empty(owner_id)
        workspace = ConversationWorkspace.model_validate(result['workspace'])
        if workspace.owner_id != owner_id:
            raise ValueError('Context owner mismatch')
        for ref, record in result['references'].items():
            validated = ReferenceRecord.model_validate(record)
            if ref != validated.token or validated.owner_id != owner_id:
                raise ValueError('Reference owner mismatch')
        return result
    except Exception as exc:
        raise ConversationStateUnavailable('No se pudo recuperar el contexto de conversación') from exc


async def save(owner_id, state):
    try:
        workspace = ConversationWorkspace.model_validate(state['workspace'])
        if workspace.owner_id != owner_id:
            raise ValueError('Context owner mismatch')
        redis = await client()
        with track_phase('redis'):
            await redis.set(key(owner_id), json.dumps(state), ex=TTL)
    except Exception as exc:
        raise ConversationStateUnavailable('No se pudo guardar el contexto de conversación') from exc


@asynccontextmanager
async def claim(owner_id):
    lock_key, token = key(owner_id) + ':claim', secrets.token_hex(16)
    try:
        redis = await client()
        with track_phase('redis'):
            acquired = await redis.set(lock_key, token, nx=True, ex=120)
    except Exception as exc:
        raise ConversationStateUnavailable('No se pudo reservar el contexto') from exc
    if not acquired:
        raise ConversationStateUnavailable('Hay otro mensaje en proceso; intentá de nuevo')
    try:
        yield
    finally:
        try:
            with track_phase('redis'):
                await redis.eval("if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0", 1, lock_key, token)
        except Exception:
            # TTL releases only this claim; a durable receipt protects committed commands.
            pass
