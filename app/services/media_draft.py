"""Turn extracted evidence into one existing movement proposal."""
import re

from app.services.assistant_contracts import DateSpec, RegisterMovement
from app.services.media_contracts import MediaExtraction, ValidatedMedia


def _date_spec(value: str | None, *, required: bool) -> DateSpec | None:
    if not value:
        return None if required else DateSpec(kind='absent')
    folded = value.strip().casefold()
    offsets = {'hoy': 0, 'ayer': -1, 'anteayer': -2}
    if folded in offsets:
        return DateSpec(kind='relative_days', evidence_span=value, day_offset=offsets[folded])
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{4})?', value):
        return DateSpec(kind='literal', evidence_span=value)
    return DateSpec(kind='unresolved', evidence_span=value)


def media_source_text(media: ValidatedMedia, extraction: MediaExtraction) -> str:
    labels = {'audio': 'nota de voz', 'image': 'foto de ticket', 'document': 'factura PDF'}
    # The fixed prefix prevents extracted text such as "sí" from becoming a user confirmation.
    return f'Adjunto recibido: {labels[media.kind]}.\nContenido reconocido: {extraction.transcript}'


def movement_draft(media: ValidatedMedia, extraction: MediaExtraction, categories: list[dict]) -> RegisterMovement:
    if extraction.ambiguous:
        raise ValueError('Encontré varios importes o movimientos posibles. Aclará cuál querés registrar; no hice cambios.')
    if media.kind == 'document' and extraction.payment_status == 'unpaid':
        raise ValueError('La factura figura pendiente de pago. Cuando la pagues, enviá el comprobante o los datos del pago; no registré un egreso.')
    category_ref = next((item['ref'] for item in categories
                         if extraction.category and item['name'].casefold() == extraction.category.casefold()), None)
    needs_category = media.kind in {'image', 'document'} and not category_ref
    return RegisterMovement(
        movement_type='egreso' if media.kind in {'image', 'document'} else extraction.movement_type,
        economic_effect=(None if media.kind == 'document' and extraction.payment_status != 'paid'
                         else ('cash_flow' if media.kind in {'image', 'document'} else extraction.economic_effect)),
        amount=str(extraction.amount) if extraction.amount is not None else None,
        currency=extraction.currency,
        description=extraction.description,
        category_ref=category_ref,
        requested_category=(extraction.category or 'Categoría del comprobante') if needs_category else None,
        date_spec=_date_spec(extraction.date_text,
                             required=media.kind == 'document'),
    )
