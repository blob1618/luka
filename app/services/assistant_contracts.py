from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from decimal import Decimal
import re

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from app.services.movement_policy import EconomicEffect


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


ReferenceToken = Annotated[str, Field(pattern=r'^ref_[0-9a-f]{32}$')]
OperationId = str  # backend: hash estable(owner, source_message, operation_key, slot)


class DatePeriod(FrozenModel):
    """Frontera pública: días civiles ISO, ambos INCLUSIVOS."""
    start: date
    end: date
    timezone: Literal['America/Argentina/Buenos_Aires'] = 'America/Argentina/Buenos_Aires'

    @model_validator(mode='after')
    def ordered(self) -> DatePeriod:
        if self.end < self.start:
            raise ValueError('period.end must be >= period.start')
        return self

    def half_open_dates(self) -> tuple[date, date]:
        """Solo adaptadores internos: no enviar este resultado al modelo."""
        return self.start, self.end + timedelta(days=1)


class PeriodReference(FrozenModel):
    """La búsqueda posterior copia ref; no reconstruye filtros ni fechas."""
    ref: ReferenceToken
    period: DatePeriod
    currency: str | None = None
    movement_type: Literal['ingreso', 'egreso'] | None = None
    label: str


class ReferenceRecord(FrozenModel):
    """Backend-only: nunca serializar al proveedor."""
    token: ReferenceToken
    owner_id: UUID
    conversation_id: str
    kind: str  # extensible: movement, category, wallet, query_scope, operation
    payload_schema: str
    payload: dict[str, JsonValue]  # validado por el esquema registrado
    created_at: datetime
    expires_at: datetime


class ScopeSelector(FrozenModel):
    kind: Literal['scope'] = 'scope'
    ref: ReferenceToken


class PeriodSelector(FrozenModel):
    kind: Literal['period'] = 'period'
    period: DatePeriod


class MonthSelector(FrozenModel):
    """Búsqueda directa por mes sin calcular su último día en el LLM."""
    kind: Literal['month'] = 'month'
    month: str = Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')

    @model_validator(mode='after')
    def valid_month(self) -> MonthSelector:
        date.fromisoformat(self.month + '-01')
        return self

    def period(self) -> DatePeriod:
        start = date.fromisoformat(self.month + '-01')
        return DatePeriod(start=start, end=start.replace(day=monthrange(start.year, start.month)[1]))


class ShownSelector(FrozenModel):
    kind: Literal['shown'] = 'shown'
    refs: tuple[ReferenceToken, ...]


class AllSelector(FrozenModel):
    kind: Literal['all'] = 'all'


MovementSelector = Annotated[
    ScopeSelector | PeriodSelector | MonthSelector | ShownSelector | AllSelector,
    Field(discriminator='kind'),
]


class FindMovementsInput(FrozenModel):
    selector: MovementSelector
    description: str | None = Field(default=None, title='el concepto del movimiento')
    category_ref: ReferenceToken | None = Field(default=None, title='la categoría que querés usar')
    currency: str | None = Field(title='la moneda', default=None, pattern=r'^[A-Z]{3}$')
    movement_type: Literal['ingreso', 'egreso'] | None = Field(default=None, title='si fue un ingreso o un gasto')
    sort_by: Literal['date_desc', 'amount_desc', 'registered_desc'] = 'date_desc'
    limit: int = Field(default=5, ge=1, le=5)


class StaleReference(ValueError):
    """An opaque handle no longer matches durable state; the user must re-read it."""


class FieldRequest(FrozenModel):
    question: str | None = Field(default=None, max_length=700)
    field: str
    reason: Literal['missing', 'ambiguous', 'invalid', 'stale']
    candidate_refs: tuple[ReferenceToken, ...] = ()


OperationStatus = Literal[
    'collecting', 'ready', 'awaiting_auth', 'suspended',
    'committed', 'cancelled', 'expired',
]
ResumableStatus = Literal['collecting', 'ready', 'awaiting_auth']


class FieldEvidence(FrozenModel):
    source_message_id: str
    reference_day: date
    source_text: str  # original del turno que aportó este campo, sellado por backend
    literal_span: str | None = None


class PendingOperation(FrozenModel):
    """Backend-only. No colección paralela de flags awaiting_*/editing."""
    operation_id: OperationId
    owner_id: UUID
    conversation_id: str
    operation_key: str
    parent_operation_id: OperationId | None = None
    schema_version: int = 1
    source_message_id: str
    original_text: str
    reference_day: date  # congelado al comenzar, no al retomar
    status: OperationStatus
    resume_status: ResumableStatus | None = None
    revision: int = Field(default=1, ge=1)
    draft_payload: dict[str, JsonValue]  # validado por OperationContract.draft_model
    field_evidence: dict[str, FieldEvidence] = Field(default_factory=dict)
    missing_fields: tuple[FieldRequest, ...] = ()
    prepared_payload: dict[str, JsonValue] | None = None  # solo backend
    prepared_hash: str | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime  # no se extiende al consultar/retomar
    commit_result: dict[str, JsonValue] | None = None
    committed_revision: int | None = None

    @model_validator(mode='after')
    def coherent(self) -> PendingOperation:
        if any(t.tzinfo is None or t.utcoffset() is None
               for t in (self.created_at, self.updated_at, self.expires_at)):
            raise ValueError('Operation timestamps must be timezone-aware')
        if (self.status == 'suspended') != (self.resume_status is not None):
            raise ValueError('Only suspended operations carry resume_status')
        effective = self.resume_status if self.status == 'suspended' else self.status
        if effective == 'ready' and (self.missing_fields or not self.prepared_hash or self.prepared_payload is None):
            raise ValueError('Ready operation requires validated prepared payload and no missing fields')
        if self.expires_at <= self.created_at or self.updated_at < self.created_at:
            raise ValueError('Invalid operation timestamps')
        return self


class PendingOperationView(FrozenModel):
    """Proyección para el modelo; sin owner/UUID, hash, snapshot o token de confirmación."""
    ref: ReferenceToken
    operation_key: str
    status: OperationStatus
    revision: int
    summary: str
    known_fields: dict[str, JsonValue]
    missing_fields: tuple[FieldRequest, ...]
    expires_at: datetime


class ConfirmationPointer(FrozenModel):
    operation_id: OperationId
    revision: int
    prepared_hash: str
    preview_source_message_id: str  # ID entrante; no inventar ID de envío de Meta
    expires_at: datetime


class ConversationWorkspace(FrozenModel):
    schema_version: Literal[2] = 2
    owner_id: UUID
    conversation_id: str
    active_operation_id: OperationId | None = None
    operations: dict[OperationId, PendingOperation] = Field(default_factory=dict)
    last_confirmation: ConfirmationPointer | None = None

    @model_validator(mode='after')
    def coherent(self) -> ConversationWorkspace:
        active = []
        for key, operation in self.operations.items():
            if (key != operation.operation_id or operation.owner_id != self.owner_id
                    or operation.conversation_id != self.conversation_id):
                raise ValueError('Operation belongs to another workspace')
            if operation.status in {'collecting', 'ready', 'awaiting_auth'}:
                active.append(key)
        if active != ([] if self.active_operation_id is None else [self.active_operation_id]):
            raise ValueError('At most one active operation; others must be suspended or terminal')
        pointer = self.last_confirmation
        if pointer:
            operation = self.operations.get(pointer.operation_id)
            if (operation is None or operation.operation_id != self.active_operation_id
                    or operation.status != 'ready' or operation.revision != pointer.revision
                    or operation.prepared_hash != pointer.prepared_hash):
                raise ValueError('Confirmation must address the current ready revision')
        return self


@dataclass(frozen=True)
class TrustedTurn:
    owner_id: UUID
    phone: str
    conversation_id: str
    inbound_message_id: str
    source_text: str
    now: datetime
    reference_day: date
    locale: str


class ConfirmationRequest(FrozenModel):
    """Entrada compacta de usuario; no es autorización para mutar por sí sola."""
    operation_id: OperationId
    revision: int = Field(ge=1)
    short_token: str = Field(pattern=r'^[0-9a-f]{16}$')
    confirmation_message_id: str


class ConfirmedAction(FrozenModel):
    """Construido SOLO por el backend a partir de una respuesta del usuario."""
    owner_id: UUID
    operation_id: OperationId
    revision: int
    prepared_hash: str
    confirmation_message_id: str


class VerifiedFact(FrozenModel):
    ref: ReferenceToken
    label: str
    value: str  # representación exacta del backend
    origin: Literal['backend_computed', 'user_provided', 'external_reported']
    required: bool = True


class DataResult(FrozenModel):
    kind: Literal['data'] = 'data'
    data: dict[str, JsonValue]  # validado por ToolContract.output_model
    facts: tuple[VerifiedFact, ...] = ()


class OperationResult(FrozenModel):
    kind: Literal['operation'] = 'operation'
    operation: PendingOperationView
    facts: tuple[VerifiedFact, ...] = ()


class ClarificationResult(FrozenModel):
    kind: Literal['clarification'] = 'clarification'
    fields: tuple[FieldRequest, ...]


class AuthRequiredResult(FrozenModel):
    kind: Literal['needs_external_auth'] = 'needs_external_auth'
    integration_key: str
    authorization_ref: ReferenceToken  # backend resuelve URL de vinculación
    operation_ref: ReferenceToken | None = None


class ToolFailure(FrozenModel):
    kind: Literal['error'] = 'error'
    code: str
    retryable: bool
    field_errors: tuple[FieldRequest, ...] = ()
    required_fact_refs: tuple[ReferenceToken, ...] = ()


ToolOutcome = Annotated[
    DataResult | OperationResult | ClarificationResult | AuthRequiredResult | ToolFailure,
    Field(discriminator='kind'),
]


class PreparedOperation(FrozenModel):
    payload: dict[str, JsonValue]  # comando sellado y snapshot de dominio
    facts: tuple[VerifiedFact, ...]
    preview: str
    parent_operation_id: str | None = None


class IncompleteOperation(FrozenModel):
    missing_fields: tuple[FieldRequest, ...]


class CommitResult(FrozenModel):
    replayed: bool
    data: dict[str, JsonValue]
    facts: tuple[VerifiedFact, ...]
    event_key: str | None = None
    event_variables: dict[str, str] = Field(default_factory=dict)



class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class DateSpec(StrictInput):
    kind: Literal['absent', 'literal', 'relative_days', 'resolved', 'unresolved'] = Field(
        description='literal: explicit calendar date quoted verbatim from the request; '
        'relative_days: relative day expression; resolved: semantic date proposal; '
        'absent: no date mentioned; unresolved: date needs clarification.')
    evidence_span: str | None = Field(default=None, description=
        'Exact verbatim substring copied from the user request, preserving date format. '
        'For request "el 16/09/2026", use "16/09/2026", never "2026-09-16". '
        'Do not normalize, translate or invent this quote; normalization is done by the backend.')
    day_offset: int | None = Field(default=None, ge=-36500, le=36500)
    proposed_iso_day: str | None = Field(default=None, description=
        'YYYY-MM-DD proposed date only for kind resolved; do not use for kind literal. '
        'evidence_span must still quote the original expression verbatim.')

    @model_validator(mode='after')
    def coherent(self):
        if self.kind == 'absent' and any(x is not None for x in (self.evidence_span, self.day_offset, self.proposed_iso_day)):
            raise ValueError('Absent date cannot carry evidence or a proposed day')
        if self.kind in {'literal', 'relative_days', 'resolved'} and not self.evidence_span:
            raise ValueError('Date evidence required')
        if self.kind == 'relative_days' and self.day_offset is None:
            raise ValueError('Relative day offset required')
        if self.kind == 'resolved' and not self.proposed_iso_day:
            raise ValueError('Resolved date required')
        if self.kind != 'relative_days' and self.day_offset is not None:
            raise ValueError('Offset is only valid for relative_days')
        if self.kind != 'resolved' and self.proposed_iso_day is not None:
            raise ValueError('Proposed day is only valid for resolved')
        return self

    def resolve(self, original: str, today: date) -> date:
        if self.evidence_span is not None and self.evidence_span not in original:
            raise ValueError('Date evidence is not in the request')
        if self.kind == 'absent':
            return today
        if self.kind == 'unresolved':
            raise ValueError('La fecha necesita un día concreto')
        if self.kind == 'relative_days':
            return today + timedelta(days=self.day_offset)
        if self.kind == 'resolved':
            return date.fromisoformat(self.proposed_iso_day)
        token = self.evidence_span
        occurrences = [m for m in re.finditer(re.escape(token), original)
                       if (m.start() == 0 or original[m.start()-1] not in '0123456789/-')
                       and (m.end() == len(original) or original[m.end()] not in '0123456789/-')]
        if not occurrences:
            raise ValueError('La fecha citada es parte de otro literal')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', token):
            return date.fromisoformat(token)
        match = re.fullmatch(r'(\d{1,2})([/\-])(\d{1,2})(?:\2(\d{4}))?', token)
        if not match:
            raise ValueError('Indicá una fecha completa, sin año de dos dígitos')
        day, _, month, year = match.groups()
        return date(int(year) if year else today.year, int(month), int(day))


class ComparePeriods(StrictInput):
    basis: Literal['month_to_date', 'full_months'] = Field(default='month_to_date',
        description='Use month_to_date for this month vs previous month: both end at the same day cutoff. '
        'Use full_months only when the user explicitly requests full calendar months, not for an ordinary current-month comparison.')
    current_month: str | None = Field(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$',
        description='Calendar month YYYY-MM, never a full YYYY-MM-DD date. Omit for the current month from backend date.')
    baseline_month: str | None = Field(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$',
        description='Calendar month YYYY-MM, never a full date. Omit for the month before current_month.')
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')


class RegisterMovement(StrictInput):
    """Typed collecting draft; missing fields are requested by the backend."""
    movement_type: Literal['ingreso', 'egreso'] | None = Field(default=None, title='si fue un ingreso o un gasto')
    economic_effect: EconomicEffect | None = Field(default=None, description=
        'Obligatorio para preparar: cash_flow sólo para dinero efectivamente cobrado/pagado (incluye intereses/comisiones separados). '
        'investment_principal, placement o existing_investment para capital/colocación/renovación o tenencia; se rechazan como ingreso/egreso. No omitir ni reclasificar principal como cash_flow.')
    amount: str | None = Field(title='el importe', default=None, pattern=r'^\d+(?:\.\d{1,10})?$')
    currency: str | None = Field(title='la moneda', default=None, pattern=r'^[A-Z]{3}$')
    description: str | None = Field(title='el concepto del movimiento', default=None, min_length=1, max_length=500)
    wallet_ref: ReferenceToken | None = Field(default=None, title='la billetera que querés usar')
    category_ref: ReferenceToken | None = Field(default=None, title='la categoría que querés usar')
    requested_category: str | None = Field(default=None, min_length=1, max_length=100,
        description='Categoría explícita que aún no existe. Mantiene collecting hasta seleccionar o crear una categoría hija; nunca crearla automáticamente.')
    date_spec: DateSpec | None = Field(default=None, title='la fecha del movimiento')

    @model_validator(mode='after')
    def positive(self):
        if self.amount is not None and Decimal(self.amount) <= 0:
            raise ValueError('Amount must be positive')
        return self


class CorrectMovementDate(StrictInput):
    movement_ref: ReferenceToken | None = Field(default=None, title='qué movimiento de los mostrados querés usar')
    date_spec: DateSpec | None = Field(default=None, title='la fecha del movimiento')


class RegisterPrepared(FrozenModel):
    economic_effect: Literal['cash_flow']
    movement_type: Literal['ingreso', 'egreso']
    amount: str = Field(pattern=r'^\d+(?:\.\d{1,10})?$')
    currency: str
    description: str
    wallet_id: str
    category_id: str | None
    date: date


class MovementSnapshot(FrozenModel):
    id: str
    amount: str
    currency: str
    description: str
    date: date
    wallet_id: str
    category_id: str
    movement_type: Literal['ingreso', 'egreso']


class CorrectDatePrepared(FrozenModel):
    movement_id: str
    expected: MovementSnapshot
    date: date


class EmptyInput(StrictInput):
    pass


class PresentResult(StrictInput):
    block_refs: tuple[ReferenceToken, ...] = ()
    presentation_style: Literal['concise', 'detail'] = 'concise'


class AskClarification(StrictInput):
    reason_code: Literal['missing_field', 'ambiguous_reference', 'unsupported_capability']
    target_field: Literal['date', 'amount', 'movement', 'wallet', 'category', 'request', 'operation']
    candidate_refs: tuple[ReferenceToken, ...] = ()


class OperationControl(StrictInput):
    ref: ReferenceToken
    expected_revision: int = Field(ge=1)


class OperationAmend(OperationControl):
    changes: dict[str, JsonValue]


class EntityReference(FrozenModel):
    id: str | None
    label: str


class ScopePayload(FrozenModel):
    period: DatePeriod
    currency: str | None = None
    movement_type: Literal['ingreso', 'egreso'] | None = None
    category_id: str | None = None
    description: str | None = None


class OperationReference(FrozenModel):
    operation_id: str


class ToolData(FrozenModel):
    """Validated JSON boundary; domain handlers use their own typed inputs."""
    status: str
    items: list[dict[str, JsonValue]] = Field(default_factory=list)


class PreviewDelivery(FrozenModel):
    operation_ref: ReferenceToken
    revision: int
    prepared_hash: str
    source_message_id: str


class ResourceReference(FrozenModel):
    id: str
    snapshot: dict[str, JsonValue]
    visible: dict[str, JsonValue]


class PendingCommand(StrictInput):
    owner_id: str
    operation_id: str
    source_message_id: str
    original_text: str
    reference_date: str
    command_kind: Literal['register', 'correct_date', 'register_batch', 'update', 'annul']
    payload: dict
    command_hash: str
    expires_at: str
    version: int = 1
    authorization_hash: str | None = None


def amount_text(value) -> str:
    """Canonical backend value; locale formatting never goes through the LLM."""
    amount = Decimal(str(value))
    return format(amount.quantize(Decimal('0.01')), ',.2f').translate(str.maketrans('.,', ',.'))

