"""Owner-scoped opaque handles and the model-facing projection."""
import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from app.models.database import Billetera, Categoria, Usuario
from app.services import finance
from app.services.assistant_contracts import EntityReference, MovementSnapshot, PreviewDelivery, ReferenceRecord, ScopePayload, StaleReference, TrustedTurn
from app.services.assistant_presentation import Presenter

if TYPE_CHECKING:
    from app.services.assistant_operations import OperationService


class ReferenceStore:
    def __init__(self, state):
        self.state = state

    def publish(self, kind, identifier, token):
        if identifier is None:
            return
        self.state.setdefault('reference_heads', {})[kind + ':' + identifier] = token
        if kind == 'movement':
            shown = []
            for ref in self.state.get('last_shown', []):
                record = self.state['references'].get(ref, {})
                item = token if record.get('kind') == kind and record.get('payload', {}).get('id') == identifier else ref
                if item and item not in shown:
                    shown.append(item)
            if token and token not in shown:
                shown.insert(0, token)
            self.state['last_shown'] = shown[:5]

    def retire(self, kind, identifier):
        self.publish(kind, identifier, '')

    def visible(self, record):
        identifier = record.payload.get('id')
        head = self.state.get('reference_heads', {}).get(record.kind + ':' + str(identifier))
        return identifier is None or head is None or head == record.token

    async def issue(self, turn, *, kind, payload_schema, payload, expires_at):
        normalized = payload.model_dump(mode='json')
        for token, value in self.state['references'].items():
            record = ReferenceRecord.model_validate(value)
            if (record.owner_id == turn.owner_id and record.conversation_id == turn.conversation_id
                    and record.kind == kind and record.payload_schema == payload_schema
                    and record.payload == normalized and record.expires_at > turn.now):
                self.publish(kind, normalized.get('id'), token)
                return token
        token = 'ref_' + uuid4().hex
        record = ReferenceRecord(token=token, owner_id=turn.owner_id,
            conversation_id=turn.conversation_id, kind=kind, payload_schema=payload_schema,
            payload=normalized, created_at=turn.now, expires_at=expires_at)
        refs = self.state['references']
        # Only expired references may be evicted; live operation dependencies survive interruption.
        for ref, value in list(refs.items()):
            if ReferenceRecord.model_validate(value).expires_at <= turn.now:
                del refs[ref]
        if len(refs) >= 300:
            raise ValueError('El contexto está lleno; esperá el vencimiento de las referencias')
        refs[token] = record.model_dump(mode='json')
        heads = self.state.setdefault('reference_heads', {})
        live_identities = {value['kind'] + ':' + value['payload']['id'] for value in refs.values()
                           if value['payload'].get('id') is not None}
        for identity, ref in list(heads.items()):
            if identity not in live_identities or (ref and ref not in refs):
                del heads[identity]
        self.publish(kind, normalized.get('id'), token)
        return token

    async def resolve(self, turn, token, *, kind, payload_model):
        value = self.state['references'].get(token)
        if value is None:
            raise StaleReference('La referencia no pertenece a este contexto')
        record = ReferenceRecord.model_validate(value)
        if (record.owner_id != turn.owner_id or record.conversation_id != turn.conversation_id
                or record.kind != kind or record.expires_at <= turn.now
                or record.payload_schema != payload_model.__name__):
            raise StaleReference('La referencia venció o no pertenece a este contexto')
        return payload_model.model_validate(record.payload)


def _entities(turn):
    with finance.SessionLocal() as session:
        if session.query(Usuario.id).filter(Usuario.id == turn.owner_id, Usuario.whatsapp_id == turn.phone).first() is None:
            raise ValueError('No hay una cuenta propia vinculada')
        categories = [(str(row.id), row.nombre) for row in session.query(Categoria).filter(
            Categoria.usuario_id == turn.owner_id, Categoria.esta_eliminado.is_(False)).all()]
        wallets = [(str(row.id), row.nombre, row.moneda) for row in session.query(Billetera).filter(
            Billetera.usuario_id == turn.owner_id).all()]
    return categories, wallets


@dataclass
class ToolContext:
    turn: TrustedTurn
    state: dict
    references: ReferenceStore
    operations: 'OperationService'
    writable: bool = True
    presenter: Presenter = field(default_factory=Presenter)
    terminal: str | None = None
    fallback_reply: str | None = None
    preview: PreviewDelivery | None = None
    preview_text: str | None = None
    outbound: list = field(default_factory=list)
    event_key: str | None = None
    event_variables: dict = field(default_factory=dict)
    clear_memory: bool = False
    action_binding: dict | None = None
    trace: list[dict] = field(default_factory=list)

    async def reference(self, kind, payload, *, expires_at=None):
        return await self.references.issue(self.turn, kind=kind, payload_schema=type(payload).__name__,
            payload=payload, expires_at=expires_at or self.turn.now + timedelta(seconds=1800))

    async def initial_context(self):
        categories, wallets = await asyncio.to_thread(_entities, self.turn)
        cats, purses = [], []
        for identifier, name in categories:
            ref = await self.reference('category', EntityReference(id=identifier, label=name))
            cats.append({'ref': ref, 'name': name})
        for identifier, name, currency in wallets:
            ref = await self.reference('wallet', EntityReference(id=identifier, label=name))
            purses.append({'ref': ref, 'name': name, 'currency': currency})
        views = await self.operations.views(self)
        visible = {}
        for ref, raw in self.state['references'].items():
            record = ReferenceRecord.model_validate(raw)
            if record.expires_at <= self.turn.now or not self.references.visible(record):
                continue
            if record.kind == 'movement':
                movement = MovementSnapshot.model_validate(record.payload)
                visible[ref] = {'kind': 'movement', 'details': movement.model_dump(mode='json', include={
                    'amount', 'currency', 'description', 'date', 'movement_type'})}
            elif record.kind == 'query_scope':
                scope = ScopePayload.model_validate(record.payload)
                visible[ref] = {'kind': 'query_scope', 'details': scope.model_dump(mode='json', exclude={'category_id'})}
            elif 'visible' in record.payload:
                visible[ref] = {'kind': record.kind, 'details': record.payload['visible']}
        return {'categories': cats, 'wallets': purses, 'operations': [v.model_dump(mode='json') for v in views],
                'references': visible,
                'last_shown': self.state.get('last_shown', []),
                'selection_options': self.state.get('selection_options', {}),
                'selected_ref': self.state.get('selected_ref')}
