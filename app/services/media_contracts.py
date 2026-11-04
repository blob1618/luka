"""Validated boundaries for inbound financial media."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


MediaKind = Literal['audio', 'image', 'document']


@dataclass(frozen=True)
class InboundMedia:
    message_id: str
    sender_phone: str
    media_id: str
    kind: MediaKind
    mime_type: str | None = None
    caption: str = ''

    @classmethod
    def from_webhook(cls, message: dict):
        kind = message.get('type')
        if kind not in {'audio', 'image', 'document'}:
            raise ValueError('Tipo de adjunto no admitido')
        body = message.get(kind)
        if not isinstance(body, dict):
            raise ValueError('Adjunto inválido')
        message_id, sender, media_id = (message.get('id'), message.get('from'), body.get('id'))
        if not all(isinstance(value, str) and value.strip() for value in (message_id, sender, media_id)):
            raise ValueError('Falta la identidad del adjunto')
        return cls(message_id, sender, media_id, kind, body.get('mime_type'),
                   str(body.get('caption') or '')[:1024])


@dataclass(frozen=True)
class ValidatedMedia:
    kind: MediaKind
    mime_type: str
    content: bytes
    caption: str = ''


class MediaExtraction(BaseModel):
    """Candidate fields from the model. None means the source did not establish the fact."""
    model_config = ConfigDict(extra='forbid')

    transcript: str = Field(default='', max_length=4000)
    amount: Decimal | None = None
    currency: Literal['ARS', 'USD', 'BRL', 'EUR'] | None = None
    movement_type: Literal['ingreso', 'egreso'] | None = None
    economic_effect: Literal['cash_flow', 'investment_principal', 'placement', 'existing_investment'] | None = None
    description: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=100)
    date_text: str | None = Field(default=None, max_length=60)
    payment_status: Literal['paid', 'unpaid', 'unknown'] = 'unknown'
    document_type: Literal['invoice', 'expense_receipt', 'bank_transfer', 'promotion', 'other'] | None = None
    ambiguous: bool = False

    @field_validator('amount')
    @classmethod
    def valid_amount(cls, value):
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError('Invalid amount') from exc
        if not amount.is_finite() or amount < 0 or amount > Decimal('1000000000000'):
            raise ValueError('Invalid amount')
        return amount or None
