"""Reminder CRUD, preferences and recurring candidates via existing services."""
import asyncio
from uuid import UUID

from pydantic import Field, field_validator

from app.models.database import CandidatoGastoRecurrente, Recordatorio, Usuario
from app.services import finance
from app.services.assistant_contracts import CommitResult, EmptyInput, FrozenModel, ReferenceToken, ResourceReference, amount_text
from app.services.assistant_capabilities.common import (
    check_resource, committed, data, issue_resource, missing, operation_tool,
    positive, prepared, read_tool, refresh_resource, require_status, snapshot_row,
)
from app.services.reminder import ReminderService
from app.services.recurring_expense import RecurringExpenseService
from app.services.currencies import is_supported, supported_text

REMINDER_FIELDS = ('titulo', 'dia_del_mes', 'monto', 'moneda', 'estado', 'candidato_id')
CANDIDATE_FIELDS = ('concepto', 'dia_estimado', 'monto_estimado', 'moneda', 'estado', 'propuesta_en', 'proxima_fecha_estimada')


class ReminderQuery(FrozenModel):
    include_paused: bool = False


class ReminderDraft(FrozenModel):
    reminder_ref: ReferenceToken | None = Field(default=None, title='qué recordatorio de los mostrados querés usar')
    title: str | None = Field(title='el título del recordatorio', default=None, min_length=1, max_length=100)
    day: int | None = Field(title='el día de vencimiento del recordatorio', default=None, ge=1, le=31)
    amount: str | None = Field(default=None, title='el importe')
    currency: str | None = Field(title='la moneda', default=None, pattern=r'^[A-Z]{3}$')
    state: str | None = Field(title='si querés pausar, activar o eliminar el recordatorio', default=None, pattern=r'^(pause|activate|delete)$')
    _positive = field_validator('amount')(positive)


class ReminderPrepared(FrozenModel):
    title: str
    day: int
    amount: str | None = None
    currency: str
    state: str | None = None
    resource: ResourceReference | None = None


class ProactiveDraft(FrozenModel):
    enabled: bool | None = Field(default=None, title='si querés recibir los avisos proactivos')


class ProactivePrepared(FrozenModel):
    enabled: bool
    previous: bool


class CandidateDraft(FrozenModel):
    candidate_ref: ReferenceToken | None = Field(default=None, title='qué propuesta recurrente querés usar')


class CandidatePrepared(FrozenModel):
    resource: ResourceReference


class CandidateDecision(FrozenModel):
    status: str
    presentation: str
    successful: bool
    replayed: bool


async def list_reminders(context, args):
    method = ReminderService.list_reminders_all if args.include_paused else ReminderService.list_reminders
    result = require_status(await asyncio.to_thread(method, context.turn.owner_id), {'ok'})
    rows, lines = [], []
    for item in result.reminders or []:
        snapshot = {key: str(item[key]) if item.get(key) is not None else None for key in REMINDER_FIELDS}
        # candidato_id is backend-only and omitted by the service's public DTO.
        def snap():
            with finance.SessionLocal() as session:
                row = session.query(Recordatorio).filter(Recordatorio.id == UUID(item['id']),
                    Recordatorio.usuario_id == context.turn.owner_id).one()
                return snapshot_row(row, REMINDER_FIELDS)
        snapshot = await asyncio.to_thread(snap)
        visible = {key: snapshot[key] for key in REMINDER_FIELDS if key != 'candidato_id'}
        ref = await issue_resource(context, 'reminder', item['id'], snapshot, visible)
        rows.append({'reminder_ref': ref, **visible})
        lines.append(f"{item['titulo']}: día {item['dia_del_mes']}, {item['estado']}." +
            (f" {item['moneda']} {amount_text(item['monto'])}." if item['monto'] is not None else ''))
    return data(context, rows, lines or ['No hay recordatorios registrados con ese estado.'])


def reminder_operation(action):
    async def prepare_reminder(context, operation):
        draft = ReminderDraft.model_validate(operation.draft_payload)
        absent = missing(draft, ('reminder_ref',) if action != 'create' else ('title', 'day'))
        if absent:
            context.event_key = 'reminder.missing_concept' if absent.missing_fields[0].field == 'title' else 'reminder.missing_day'
            context.event_variables = {'concept': draft.title or 'el pago'}
            return absent
        resource = await check_resource(context, draft.reminder_ref, 'reminder', Recordatorio, REMINDER_FIELDS) if draft.reminder_ref else None
        old = resource.snapshot if resource else {}
        title, day = draft.title or old.get('titulo'), draft.day or old.get('dia_del_mes')
        currency = draft.currency or old.get('moneda') or 'ARS'
        if not is_supported(currency):
            raise ValueError(f'Moneda no soportada. Usá {supported_text()}.')
        amount = draft.amount if 'amount' in operation.field_evidence else old.get('monto')
        if action == 'change_reminder_state' and draft.state is None:
            return missing(draft, ('state',))
        def duplicate():
            with finance.SessionLocal() as session:
                query = session.query(Recordatorio.id).filter(Recordatorio.usuario_id == context.turn.owner_id,
                    Recordatorio.titulo == title, Recordatorio.estado != 'eliminado')
                if resource:
                    query = query.filter(Recordatorio.id != UUID(resource.id))
                return query.first() is not None
        if action != 'change_reminder_state' and await asyncio.to_thread(duplicate):
            from app.services.assistant_contracts import FieldRequest, IncompleteOperation
            context.event_key, context.event_variables = 'reminder.duplicate', {'title': title}
            return IncompleteOperation(missing_fields=(FieldRequest(field='title', reason='ambiguous', question='Ya existe un recordatorio con ese título. Indicá un nombre diferente.'),))
        value = ReminderPrepared(title=title, day=int(day), currency=currency, amount=amount,
            state=draft.state, resource=resource)
        preview = ({'create': 'Crear recordatorio', 'update': 'Actualizar recordatorio',
                    'change_reminder_state': 'Cambiar estado de'}[action]
                   + f': {title}, día {day}, {currency}'
                   + (f' {amount_text(amount)}' if amount else ', sin importe'))
        if draft.state:
            preview += ', ' + {'pause': 'pausar', 'activate': 'reactivar',
                               'delete': 'eliminar'}.get(draft.state, draft.state)
        return prepared(value, preview + '.')
    async def commit_reminder(context, operation, confirmation):
        value = ReminderPrepared.model_validate(operation.prepared_payload)
        await prepare_reminder(context, operation)
        arguments = {'reminder_concept': value.title, 'reminder_day': value.day,
            'reminder_amount': value.amount, 'reminder_currency': value.currency}
        if action == 'create':
            result = await asyncio.to_thread(ReminderService.create_reminder, context.turn.phone, arguments)
            status = 'created'
        elif action == 'update':
            result = await asyncio.to_thread(ReminderService.update_reminder, context.turn.phone, value.resource.id, arguments)
            status = 'updated'
        else:
            method, status = {'pause': (ReminderService.pause_reminder, 'paused'),
                'activate': (ReminderService.activate_reminder, 'activated'), 'delete': (ReminderService.delete_reminder, 'deleted')}[value.state]
            result = await asyncio.to_thread(method, context.turn.phone, value.resource.id)
        require_status(result, {status, 'already_' + status})
        identifier = result.reminder_id or (value.resource.id if value.resource else None)
        if status == 'deleted':
            context.references.retire('reminder', identifier)
        else:
            await refresh_resource(context, 'reminder', Recordatorio, identifier, REMINDER_FIELDS,
                lambda snapshot: {key: item for key, item in snapshot.items() if key != 'candidato_id'})
        verb = {'created': 'creado', 'updated': 'actualizado', 'paused': 'pausado',
                'activated': 'reactivado', 'deleted': 'eliminado'}.get(status, status)
        if result.status == 'already_' + status:
            verb = 'sin cambios'
        return committed(context, f'{value.title}: {verb}.', event='reminder.' + status,
            variables={'title': value.title, 'day': str(value.day), 'amount': amount_text(value.amount) if value.amount else 'sin importe', 'currency': value.currency})
    key = {'create': 'create_reminder', 'update': 'update_reminder', 'change_reminder_state': 'change_reminder_state'}[action]
    return operation_tool(key, 'Prepara '+key+' propio con aprobación. Para gestionar pausados consultá list_reminders include_paused=true. '
        'Usá reminder_ref del listado; título duplicado y campos faltantes se aclaran en el mismo borrador.',
        ReminderDraft, ReminderPrepared, prepare_reminder, commit_reminder)


def _proactive(owner):
    with finance.SessionLocal() as session:
        return bool(session.query(Usuario.proactivo_habilitado).filter(Usuario.id == owner).scalar())


async def prepare_proactive(context, operation):
    draft = ProactiveDraft.model_validate(operation.draft_payload)
    absent = missing(draft, ('enabled',))
    if absent:
        return absent
    previous = await asyncio.to_thread(_proactive, context.turn.owner_id)
    if operation.prepared_payload and previous != operation.prepared_payload['previous']:
        raise ValueError('La preferencia cambió')
    return prepared(ProactivePrepared(enabled=draft.enabled, previous=previous),
        'Activar avisos proactivos.' if draft.enabled else 'Desactivar avisos proactivos.')


async def commit_proactive(context, operation, confirmation):
    await prepare_proactive(context, operation)
    value = ProactivePrepared.model_validate(operation.prepared_payload)
    require_status(await asyncio.to_thread(ReminderService.set_proactive_prompts, context.turn.phone, value.enabled), {'updated'})
    return committed(context, 'Avisos proactivos activados.' if value.enabled else 'Avisos proactivos desactivados.')


async def list_candidates(context, args):
    def query():
        with finance.SessionLocal() as session:
            rows = session.query(CandidatoGastoRecurrente).filter(CandidatoGastoRecurrente.usuario_id == context.turn.owner_id,
                CandidatoGastoRecurrente.estado == 'pendiente').all()
            return [(str(row.id), snapshot_row(row, CANDIDATE_FIELDS)) for row in rows
                if not RecurringExpenseService.is_proposal_expired(row)]
    rows, lines = [], []
    for identifier, snapshot in await asyncio.to_thread(query):
        visible = {key: value for key, value in snapshot.items() if key != 'propuesta_en'}
        ref = await issue_resource(context, 'recurring_candidate', identifier, snapshot, visible)
        rows.append({'candidate_ref': ref, **visible})
        lines.append(f"{snapshot['concepto']}: día {snapshot['dia_estimado']}, {snapshot['moneda']} {amount_text(snapshot['monto_estimado'])}.")
    return data(context, rows, lines or ['No hay propuestas recurrentes vigentes.'])


def candidate_operation(accept):
    async def prepare_candidate(context, operation):
        draft = CandidateDraft.model_validate(operation.draft_payload)
        absent = missing(draft, ('candidate_ref',))
        if absent:
            return absent
        resource = await check_resource(context, draft.candidate_ref, 'recurring_candidate', CandidatoGastoRecurrente, CANDIDATE_FIELDS)
        def eligible():
            with finance.SessionLocal() as session:
                row = session.get(CandidatoGastoRecurrente, UUID(resource.id))
                return row.estado == 'pendiente' and not RecurringExpenseService.is_proposal_expired(row)
        if not await asyncio.to_thread(eligible):
            raise ValueError('La propuesta recurrente ya no está vigente')
        return prepared(CandidatePrepared(resource=resource), ('Aceptar' if accept else 'Rechazar') + f" propuesta recurrente {resource.visible['concepto']}.")
    async def commit_candidate(context, operation, confirmation):
        value = CandidatePrepared.model_validate(operation.prepared_payload)
        result = await apply_candidate(context.turn.owner_id, value.resource.id, accept)
        if not result.successful:
            raise ValueError(result.presentation)
        return CommitResult(replayed=result.replayed, data={'presentation': result.presentation,
            'candidate_status': result.status}, facts=())
    return operation_tool('accept_recurring_candidate' if accept else 'reject_recurring_candidate',
        'Prepara decisión explícita sobre propuesta recurrente propia, vigente y mostrada. Nunca ejecuta la decisión desde el modelo.',
        CandidateDraft, CandidatePrepared, prepare_candidate, commit_candidate)


async def apply_candidate(owner, identifier, accept):
    def execute():
        with finance.SessionLocal() as session:
            method = RecurringExpenseService.convert_candidate_to_reminder if accept else RecurringExpenseService.reject_candidate
            status, resource = method(session, owner, UUID(identifier))
            if status == 'converted' and resource is not None:
                text = (f'¡Listo! Agendé el recordatorio para *{resource.titulo}* los días {resource.dia_del_mes} '
                        f'de cada mes con aviso {resource.dias_anticipacion} días antes.')
            else:
                texts = {'already_converted': 'Ya tenés este recordatorio activo en tu cuenta.',
                    'rejected': 'Entendido, no volveré a sugerirte este recordatorio.',
                    'already_rejected': 'Esta sugerencia ya había sido rechazada.' if accept else 'Esta sugerencia ya había sido rechazada anteriormente.',
                    'expired': 'Esta sugerencia ya venció. Si querés, podés crear un recordatorio manualmente escribiéndome.' if accept else 'Esta sugerencia ya había vencido.',
                    'conflict': 'No se pudo aplicar el cambio debido a un conflicto con el estado actual de la sugerencia.' if accept else 'No se pudo rechazar la sugerencia porque ya tenés este recordatorio configurado.',
                    'unavailable': 'Esta sugerencia ya no está disponible.',
                    'not_found': 'No encontré la sugerencia solicitada.'}
                text = texts.get(status, 'No pude procesar la decisión del recordatorio. Intentá más tarde.')
            return CandidateDecision(status=status, presentation=text,
                successful=status in {'converted', 'already_converted', 'rejected', 'already_rejected'},
                replayed=status.startswith('already_'))
    return await asyncio.to_thread(execute)


def reminder_management_contracts():
    return (read_tool('list_reminders', 'Lista propios activos; include_paused=true incluye pausados para gestionar. Devuelve refs visibles persistentes.', ReminderQuery, list_reminders),
        *(reminder_operation(action) for action in ('create', 'update', 'change_reminder_state')),
        operation_tool('set_proactive_prompts', 'Prepara activar/desactivar avisos proactivos propios.', ProactiveDraft, ProactivePrepared, prepare_proactive, commit_proactive),
        read_tool('list_recurring_candidates', 'Lista propuestas recurrentes propias vigentes con refs.', EmptyInput, list_candidates),
        candidate_operation(True), candidate_operation(False))
