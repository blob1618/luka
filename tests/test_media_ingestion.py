"""Offline evidence for media validation, proposals and confirmed persistence."""
from io import BytesIO
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pypdf import PdfWriter

from app.api import whatsapp_media
from app.api.whatsapp import InboundInteractiveReply, WhatsAppReplyButtons
from app.models.database import MovimientoFinanciero, Usuario
from app.services import assistant_runtime, assistant_state, dispatcher, media_extraction
from app.services.media_contracts import InboundMedia, MediaExtraction, ValidatedMedia
from app.services.media_draft import movement_draft
from app.services.webhook_idempotency import process_interactive_message_once, process_media_message_once, process_text_message_once
from tests.test_assistant_pilot import PHONE, NativeScript, amend_active, accounts as _accounts_fixture

accounts = _accounts_fixture

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind,mime,content', [
    ('image', 'image/jpeg', b'not an image'),
    ('audio', 'audio/ogg', b'not an audio file'),
    ('document', 'application/pdf', b'%PDF-broken'),
])
def test_rejects_corrupt_or_mislabeled_media(kind, mime, content):
    with pytest.raises(whatsapp_media.MediaError):
        whatsapp_media._validate_content(kind, mime, content)


def test_accepts_small_valid_pdf_and_rejects_encrypted_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    out = BytesIO()
    writer.write(out)
    whatsapp_media._validate_content('document', 'application/pdf', out.getvalue())
    writer.encrypt('secret')
    locked = BytesIO()
    writer.write(locked)
    with pytest.raises(whatsapp_media.MediaError, match='protegido'):
        whatsapp_media._validate_content('document', 'application/pdf', locked.getvalue())


@pytest.mark.asyncio
async def test_download_checks_auth_mime_and_bytes(monkeypatch):
    monkeypatch.setenv('WHATSAPP_API_TOKEN', 'test-token')
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == 'graph.facebook.com':
            return httpx.Response(200, json={'mime_type': 'image/jpeg', 'file_size': 6,
                'url': 'https://lookaside.fbsbx.com/fixture'})
        return httpx.Response(200, content=b'\xff\xd8\xffx\xff\xd9')

    original = httpx.AsyncClient
    monkeypatch.setattr(whatsapp_media.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    inbound = InboundMedia('wamid.image', PHONE, 'media-1', 'image', 'image/jpeg')
    media = await whatsapp_media.download_media(inbound)
    assert media.content == b'\xff\xd8\xffx\xff\xd9'
    assert all(request.headers['authorization'] == 'Bearer test-token' for request in requests)


@pytest.mark.asyncio
async def test_extractor_requires_literal_date_evidence(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')

    def handler(request):
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text':
            '{"transcript":"Pagué luz","amount":25000,"currency":"ARS",'
            '"movement_type":"egreso","economic_effect":"cash_flow",'
            '"description":"Luz","category":"Servicios","date_text":"ayer",'
            '"payment_status":"paid","ambiguous":false}'}]}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(media_extraction.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(media_extraction.ExtractionError, match='fecha'):
        await media_extraction.extract_media(ValidatedMedia('audio', 'audio/ogg', b'OggSx'))


@pytest.mark.asyncio
async def test_extractor_reports_provider_timeout(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')

    def handler(request):
        raise httpx.ReadTimeout('provider did not answer', request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(media_extraction.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(media_extraction.ExtractionError, match='tardó demasiado'):
        await media_extraction.extract_media(ValidatedMedia('document', 'application/pdf', b'%PDF-x'))


def test_invoice_requires_payment_and_ticket_requires_category():
    invoice = ValidatedMedia('document', 'application/pdf', b'%PDF-x')
    extracted = MediaExtraction(transcript='Factura luz 25000', amount='25000', currency='ARS',
                                description='Luz', category='Servicios', document_type='invoice')
    draft = movement_draft(invoice, extracted, [])
    assert draft.economic_effect is None and draft.date_spec is None
    ticket = ValidatedMedia('image', 'image/jpeg', b'\xff\xd8\xff\xd9')
    categorized = movement_draft(ticket, extracted, [])
    assert categorized.requested_category == 'Servicios' and categorized.category_ref is None


@pytest.mark.parametrize('document_type,ambiguous,expected', [
    ('bank_transfer', False, 'transferencia'),
    ('promotion', True, 'promoción'),
    ('other', False, 'factura o recibo'),
    (None, False, 'factura o recibo'),
])
def test_non_expense_pdf_cannot_prepare_movement(document_type, ambiguous, expected):
    media = ValidatedMedia('document', 'application/pdf', b'%PDF-x')
    extracted = MediaExtraction(transcript='Documento con importe', amount='100000', currency='ARS',
                                payment_status='paid', document_type=document_type, ambiguous=ambiguous)
    with pytest.raises(ValueError, match=expected):
        movement_draft(media, extracted, [])


def test_paid_expense_receipt_can_prepare_movement():
    media = ValidatedMedia('document', 'application/pdf', b'%PDF-x')
    extracted = MediaExtraction(transcript='Pago 30/10/2026, total 2200', amount='2200', currency='ARS',
                                description='Viaje', category='Transporte', date_text='30/10/2026',
                                payment_status='paid', document_type='expense_receipt')
    draft = movement_draft(media, extracted, [])
    assert draft.amount == '2200' and draft.economic_effect == 'cash_flow'
    assert draft.date_spec.evidence_span == '30/10/2026'


@pytest.mark.asyncio
@pytest.mark.parametrize('document_type,expected', [
    ('bank_transfer', 'transferencia'),
    ('promotion', 'promoción'),
])
async def test_non_expense_pdf_sends_rejection_without_operation(
    accounts, fake_redis, document_type, expected,
):
    factory, owner, *_ = accounts
    with factory() as session:
        session.get(Usuario, owner).auth_user_id = uuid4()
        session.commit()
        before = session.query(MovimientoFinanciero).filter_by(usuario_id=owner).count()
    inbound = InboundMedia(f'wamid.{document_type}', PHONE, 'pdf', 'document', 'application/pdf')

    async def load(_):
        return ValidatedMedia('document', 'application/pdf', b'fixture')

    async def extract(_):
        return MediaExtraction(transcript='PDF con importe', amount='100000', currency='ARS',
                               payment_status='paid', document_type=document_type)

    async def process(**kwargs):
        return await dispatcher.process_incoming_media(**kwargs, load_media=load, extract=extract)

    sent = []
    async def deliver(_, message):
        sent.append(message)
        return True

    assert await process_media_message_once(redis_client=fake_redis, inbound_media=inbound,
        process_media=process, send_message=deliver) == 'completed'
    assert any(expected in str(message) for message in sent)
    state = await assistant_state.read(owner)
    assert not state['workspace']['operations']
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=owner).count() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,mime,transcript,date_text,payment_status', [
    ('audio', 'audio/ogg', 'Ayer gasté 9000 en comida', 'Ayer', 'unknown'),
    ('image', 'image/jpeg', 'Comida total 9000', None, 'unknown'),
    ('document', 'application/pdf', 'Factura comida pagada el 04/11/2026, total 9000', '04/11/2026', 'paid'),
])
async def test_media_needs_delivered_preview_then_commits_once(
    accounts, fake_redis, monkeypatch, kind, mime, transcript, date_text, payment_status,
):
    factory, owner, *_ = accounts
    with factory() as session:
        session.get(Usuario, owner).auth_user_id = uuid4()
        session.commit()
    inbound = InboundMedia('wamid.ticket', PHONE, 'test-media', kind, mime)
    extracted = MediaExtraction(transcript=transcript, amount='9000', currency='ARS',
        movement_type='egreso', economic_effect='cash_flow', description='Compra comida', category='Comida',
        date_text=date_text, payment_status=payment_status,
        document_type='invoice' if kind == 'document' else None)

    async def load(_):
        return ValidatedMedia(kind, mime, b'fixture')

    async def extract(_):
        return extracted

    async def process_media(**kwargs):
        return await dispatcher.process_incoming_media(**kwargs, load_media=load, extract=extract)

    sent = []
    async def deliver(_, message):
        sent.append(message)
        return True

    kwargs = dict(redis_client=fake_redis, inbound_media=inbound,
                  process_media=process_media, send_message=deliver)
    with factory() as session:
        before = session.query(MovimientoFinanciero).filter_by(usuario_id=owner).count()
    assert await process_media_message_once(**kwargs) == 'completed'
    assert await process_media_message_once(**kwargs) == 'duplicate'
    preview = next(message for message in sent if isinstance(message, WhatsAppReplyButtons))
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=owner).count() == before
    action = next(button.id for button in preview.buttons if button.id.startswith('assistant:confirm:'))
    response = await process_interactive_message_once(redis_client=fake_redis,
        interactive_reply=InboundInteractiveReply('wamid.confirm', PHONE, 'button_reply', action, 'Confirmar'),
        process_reply=dispatcher.process_incoming_interactive_reply, send_message=deliver)
    assert response == 'completed'
    with factory() as session:
        rows = session.query(MovimientoFinanciero).filter_by(usuario_id=owner, descripcion='Compra comida').all()
        assert len(rows) == 1 and rows[0].cantidad == Decimal('9000')


@pytest.mark.asyncio
async def test_media_text_cannot_confirm_previous_proposal(accounts, fake_redis, monkeypatch):
    factory, owner, *_ = accounts
    with factory() as session:
        session.get(Usuario, owner).auth_user_id = uuid4()
        session.commit()
    first = InboundMedia('wamid.first', PHONE, 'm1', 'audio', 'audio/ogg')
    second = InboundMedia('wamid.second', PHONE, 'm2', 'audio', 'audio/ogg')
    async def load(_):
        return ValidatedMedia('audio', 'audio/ogg', b'OggSx')
    async def extract(media):
        return MediaExtraction(transcript='sí', amount='100', currency='ARS', movement_type='egreso',
                               economic_effect='cash_flow', description='Taxi')
    async def process_media(**kwargs):
        return await dispatcher.process_incoming_media(**kwargs, load_media=load, extract=extract)
    sent = []
    async def deliver(_, message):
        sent.append(message)
        return True
    for inbound in (first, second):
        await process_media_message_once(redis_client=fake_redis, inbound_media=inbound,
                                         process_media=process_media, send_message=deliver)
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=owner, descripcion='Taxi').count() == 0
    state = await assistant_state.read(owner)
    active = state['workspace']['active_operation_id']
    assert state['workspace']['operations'][active]['status'] == 'ready'


@pytest.mark.asyncio
async def test_unknown_invoice_payment_keeps_collecting_draft(accounts, fake_redis):
    factory, owner, *_ = accounts
    with factory() as session:
        session.get(Usuario, owner).auth_user_id = uuid4()
        session.commit()
    inbound = InboundMedia('wamid.invoice', PHONE, 'pdf', 'document', 'application/pdf')
    async def load(_):
        return ValidatedMedia('document', 'application/pdf', b'fixture')
    async def extract(_):
        return MediaExtraction(transcript='Factura de luz total 25000, vence mañana', amount='25000',
            currency='ARS', description='Luz', category='Comida', payment_status='unknown',
            document_type='invoice')
    sent = []
    async def deliver(_, message):
        sent.append(message)
        return True
    async def process(**kwargs):
        return await dispatcher.process_incoming_media(**kwargs, load_media=load, extract=extract)
    assert await process_media_message_once(redis_client=fake_redis, inbound_media=inbound,
        process_media=process, send_message=deliver) == 'completed'
    state = await assistant_state.read(owner)
    active = state['workspace']['active_operation_id']
    operation = state['workspace']['operations'][active]
    assert operation['status'] == 'collecting'
    assert operation['draft_payload']['economic_effect'] is None
    assert operation['draft_payload']['date_spec'] is None
    assert any('¿Ya pagaste' in str(message) for message in sent)
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=owner, descripcion='Luz').count() == 0


@pytest.mark.asyncio
async def test_failed_preview_delivery_can_retry_without_reextracting(accounts, fake_redis):
    factory, owner, *_ = accounts
    with factory() as session:
        session.get(Usuario, owner).auth_user_id = uuid4()
        session.commit()
    inbound = InboundMedia('wamid.retry', PHONE, 'm1', 'image', 'image/jpeg')
    calls = 0
    async def load(_):
        return ValidatedMedia('image', 'image/jpeg', b'fixture')
    async def extract(_):
        nonlocal calls
        calls += 1
        return MediaExtraction(transcript='Comida total 9000', amount='9000', currency='ARS',
            description='Compra comida', category='Comida')
    async def process(**kwargs):
        return await dispatcher.process_incoming_media(**kwargs, load_media=load, extract=extract)
    async def fail_delivery(_, __):
        return False
    with pytest.raises(RuntimeError, match='could not be sent'):
        await process_media_message_once(redis_client=fake_redis, inbound_media=inbound,
            process_media=process, send_message=fail_delivery)
    delivered = []
    async def deliver(_, message):
        delivered.append(message)
        return True
    assert await process_media_message_once(redis_client=fake_redis, inbound_media=inbound,
        process_media=process, send_message=deliver) == 'completed'
    assert calls == 1
    assert any(isinstance(message, WhatsAppReplyButtons) for message in delivered)
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=owner, descripcion='Compra comida').count() == 0


def test_media_download_rejects_untrusted_url_and_oversize():
    assert not whatsapp_media._safe_media_url('http://127.0.0.1/private')
    assert not whatsapp_media._safe_media_url('https://facebook.com.evil.test/file')
    assert not whatsapp_media._safe_media_url('https://facebook.com:8443/file')
    with pytest.raises(whatsapp_media.MediaError, match='5 MiB'):
        whatsapp_media._validate_content('image', 'image/jpeg', b'\xff\xd8\xff' + b'x' * (whatsapp_media.MAX_MEDIA_BYTES + 1))


@pytest.mark.asyncio
async def test_unknown_user_is_gated_before_media_download(accounts):
    called = False
    async def load(_):
        nonlocal called
        called = True
        raise AssertionError('download must not be called')
    inbound = InboundMedia('wamid.unknown', '5491199999999', 'media', 'audio', 'audio/ogg')
    result = await dispatcher.process_incoming_media(inbound.sender_phone, inbound, load_media=load)
    assert not called
    assert result.service_invoked == 'onboarding'


@pytest.mark.asyncio
async def test_invoice_clarification_prepares_paid_expense(accounts, fake_redis, monkeypatch):
    factory, owner, *_ = accounts
    with factory() as session:
        session.get(Usuario, owner).auth_user_id = uuid4()
        session.commit()
    inbound = InboundMedia('wamid.invoice.followup', PHONE, 'pdf', 'document', 'application/pdf')
    async def load(_):
        return ValidatedMedia('document', 'application/pdf', b'fixture')
    async def extract(_):
        return MediaExtraction(transcript='Factura por comida, total 25000, vence mañana',
            amount='25000', currency='ARS', description='Factura comida', category='Comida',
            payment_status='unknown', document_type='invoice')
    async def process(**kwargs):
        return await dispatcher.process_incoming_media(**kwargs, load_media=load, extract=extract)
    sent = []
    async def deliver(_, message):
        sent.append(message)
        return True
    await process_media_message_once(redis_client=fake_redis, inbound_media=inbound,
        process_media=process, send_message=deliver)
    monkeypatch.setattr(assistant_runtime, 'create_provider', lambda: NativeScript([
        amend_active({'economic_effect': 'cash_flow', 'date_spec': {
            'kind': 'relative_days', 'evidence_span': 'ayer', 'day_offset': -1}})]))
    assert await process_text_message_once(redis_client=fake_redis, sender_phone=PHONE,
        text_body='Sí, la pagué ayer', whatsapp_message_id='wamid.invoice.paid',
        process_message=dispatcher.process_incoming_message, send_message=deliver) == 'completed'
    preview = next(message for message in sent if isinstance(message, WhatsAppReplyButtons))
    action = next(button.id for button in preview.buttons if button.id.startswith('assistant:confirm:'))
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=owner, descripcion='Factura comida').count() == 0
    assert await process_interactive_message_once(redis_client=fake_redis,
        interactive_reply=InboundInteractiveReply('wamid.invoice.confirm', PHONE, 'button_reply', action, 'Confirmar'),
        process_reply=dispatcher.process_incoming_interactive_reply, send_message=deliver) == 'completed'
    with factory() as session:
        assert session.query(MovimientoFinanciero).filter_by(usuario_id=owner, descripcion='Factura comida').count() == 1
