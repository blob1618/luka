"""Dispatcher integration for query_usd_quotes (STK-45 / STK-217).

Every test drives the public message pipeline, so the LLM normalizer runs
before the dispatcher. The intent only reports quote data: no balances, no
wallets and no patrimonio calculation.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.models.database import Billetera, MovimientoFinanciero, Usuario
from app.services.currency import QuoteType, QuoteUnavailableError, UsdQuote
from tests.tool_scenarios import execute_tool
from app.services.llm import LLMService
from app.services.onboarding import OnboardingDecision, OnboardingResult
pytestmark = pytest.mark.unit
PHONE = '5491100000301'
EARLY = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
LATE = datetime(2026, 10, 23, 15, 30, tzinfo=timezone.utc)
RENDERED_REPLY = '• cotizaciones redactadas'

def known_user() -> OnboardingResult:
    return OnboardingResult(OnboardingDecision.KNOWN_USER)

def quote(tipo: QuoteType, compra: str | None='1000', venta: str='1100', *, stale: bool=False, fuente: str='DolarAPI', fetched_at: datetime=EARLY) -> UsdQuote:
    return UsdQuote(tipo=tipo, compra=Decimal(compra) if compra is not None else None, venta=Decimal(venta), fuente=fuente, as_of=fetched_at, fetched_at=fetched_at, stale=stale)

def all_three_quotes() -> dict[QuoteType, UsdQuote]:
    return {QuoteType.OFICIAL: quote(QuoteType.OFICIAL, '1000', '1100'), QuoteType.MEP: quote(QuoteType.MEP, '1100', '1200'), QuoteType.BLUE: quote(QuoteType.BLUE, '1200', '1300')}

def seed_user(session, phone: str=PHONE) -> int:
    user = Usuario(nombre=phone, email=f'{uuid.uuid4()}@example.com', whatsapp_id=phone)
    session.add(user)
    session.flush()
    wallet = Billetera(usuario_id=user.id, nombre='Efectivo', moneda='ARS')
    session.add(wallet)
    session.flush()
    session.add(MovimientoFinanciero(billetera_id=wallet.id, usuario_id=user.id, tipo='ingreso', cantidad=Decimal('80000'), moneda='ARS', descripcion='seed', fecha_movimiento=date(2026, 10, 1)))
    session.commit()
    return user.id

async def ask(arguments, *, text='Cotización de prueba', quotes=None, quotes_error=None):
    from app.services import finance
    with finance.SessionLocal() as session:
        if session.query(Usuario).filter_by(whatsapp_id=PHONE).first() is None:
            seed_user(session, PHONE)
    render = AsyncMock(return_value=RENDERED_REPLY)
    get_quotes = AsyncMock(side_effect=quotes_error) if quotes_error else AsyncMock(return_value=quotes or {})
    with patch.object(LLMService, 'render_financial_reply', render), patch('app.services.assistant_capabilities.financial_reads.get_usd_quotes', get_quotes):
        result = await execute_tool(PHONE, 'query_usd_quotes', arguments, text=text)
    return (result, render, get_quotes)

def facts_and_types(render: AsyncMock) -> tuple[dict[str, str], list[str]]:
    args = render.await_args
    assert args.args[0] == 'usd_quotes'
    facts = {fact['key']: fact['value'] for fact in args.args[1]}
    return (facts, args.args[2])

@pytest.mark.asyncio
async def test_quote_detail_returns_only_quotes(isolated_db):
    quotes = {QuoteType.BLUE: quote(QuoteType.BLUE, '1545.25', '1565.75')}
    result, render, _get_quotes = await ask({'quote_types': ['blue']}, quotes=quotes)
    assert result.service_invoked == 'assistant'
    assert RENDERED_REPLY in result.reply_text
    facts, tipos = facts_and_types(render)
    assert tipos == ['blue']
    assert facts['BLUE_COMPRA'] == '1.545,25'
    assert facts['BLUE_VENTA'] == '1.565,75'
    assert not any((key.startswith(('ARS_', 'USD_')) for key in facts))

@pytest.mark.asyncio
async def test_default_quote_types_requests_all_three_without_net_worth(isolated_db):
    calculate = MagicMock()
    with patch('app.services.assistant_capabilities.financial_reads.calculate_net_worth', calculate):
        result, render, get_quotes = await ask({}, quotes=all_three_quotes())
    get_quotes.assert_awaited_once_with([QuoteType.OFICIAL, QuoteType.MEP, QuoteType.BLUE])
    calculate.assert_not_called()
    facts, tipos = facts_and_types(render)
    assert result.service_invoked == 'assistant'
    assert tipos == ['oficial', 'mep', 'blue']
    assert {'OFICIAL_COMPRA', 'MEP_COMPRA', 'BLUE_COMPRA'} <= set(facts)

@pytest.mark.asyncio
async def test_requested_subset_only_produces_those_types(isolated_db):
    _result, render, get_quotes = await ask({'quote_types': ['oficial', 'mep']}, quotes=all_three_quotes())
    get_quotes.assert_awaited_once_with([QuoteType.OFICIAL, QuoteType.MEP])
    facts, tipos = facts_and_types(render)
    assert tipos == ['oficial', 'mep']
    assert not any((key.startswith('BLUE_') for key in facts))
    assert not any((key.startswith(('ARS_', 'USD_')) for key in facts))

@pytest.mark.asyncio
async def test_partial_quotes_report_missing_type_as_unavailable(isolated_db):
    _result, render, _get_quotes = await ask({'quote_types': ['oficial', 'blue']}, quotes={QuoteType.BLUE: quote(QuoteType.BLUE, '1200', '1300')})
    facts, _tipos = facts_and_types(render)
    assert facts['OFICIAL_STATUS'] == 'cotización no disponible'
    assert 'OFICIAL_VENTA' not in facts
    assert facts['BLUE_VENTA'] == '1.300'
    assert 'BLUE_STATUS' not in facts

@pytest.mark.asyncio
async def test_missing_compra_keeps_venta(isolated_db):
    _result, render, _get_quotes = await ask({'quote_types': ['blue']}, quotes={QuoteType.BLUE: quote(QuoteType.BLUE, None, '1565.75')})
    facts, _tipos = facts_and_types(render)
    assert facts['BLUE_COMPRA'] == 'no disponible'
    assert facts['BLUE_VENTA'] == '1.565,75'

@pytest.mark.asyncio
async def test_stale_quote_is_flagged(isolated_db):
    _result, render, _get_quotes = await ask({'quote_types': ['blue']}, quotes={QuoteType.BLUE: quote(QuoteType.BLUE, '1200', '1300', stale=True)})
    facts, _tipos = facts_and_types(render)
    assert facts['BLUE_STALE'] == 'cotización desactualizada'

@pytest.mark.asyncio
async def test_unavailable_quotes_report_status_without_source_or_values(isolated_db):
    result, render, _get_quotes = await ask({}, quotes_error=QuoteUnavailableError('caído'))
    facts, tipos = facts_and_types(render)
    assert tipos == ['oficial', 'mep', 'blue']
    for tipo in ('OFICIAL', 'MEP', 'BLUE'):
        assert facts[f'{tipo}_STATUS'] == 'cotización no disponible'
        assert f'{tipo}_VENTA' not in facts
    assert 'SOURCE' not in facts
    assert 'FETCHED_AT' not in facts
    assert RENDERED_REPLY in result.reply_text

@pytest.mark.asyncio
async def test_source_and_fetched_at_use_real_maximum(isolated_db):
    quotes = {QuoteType.OFICIAL: quote(QuoteType.OFICIAL, '1000', '1100', fuente='DolarAPI', fetched_at=EARLY), QuoteType.BLUE: quote(QuoteType.BLUE, '1200', '1300', fuente='DolarAPI', fetched_at=LATE)}
    _result, render, _get_quotes = await ask({'quote_types': ['oficial', 'blue']}, quotes=quotes)
    facts, _tipos = facts_and_types(render)
    assert facts['SOURCE'] == 'DolarAPI'
    assert facts['FETCHED_AT'] == LATE.isoformat()

@pytest.mark.asyncio
async def test_quote_detail_writes_no_movements_or_wallets(isolated_db):
    with isolated_db() as session:
        seed_user(session)
    _result, _render, _get_quotes = await ask({'quote_types': ['blue']}, quotes={QuoteType.BLUE: quote(QuoteType.BLUE, '1200', '1300')})
    with isolated_db() as session:
        assert session.query(MovimientoFinanciero).count() == 1
        assert session.query(Billetera).count() == 1

@pytest.mark.asyncio
async def test_explicit_quote_request_is_not_intercepted_by_education(isolated_db):
    result, render, _get_quotes = await ask({'quote_types': ['blue']}, text='cotización actual del blue', quotes={QuoteType.BLUE: quote(QuoteType.BLUE, '1200', '1300')})
    assert result.service_invoked == 'assistant'
    render.assert_awaited_once()
    assert RENDERED_REPLY in result.reply_text

@pytest.mark.asyncio
async def test_quote_definition_remains_educational(isolated_db):
    with isolated_db() as session:
        seed_user(session)
    render = AsyncMock()
    with patch.object(LLMService, 'render_financial_reply', render):
        result = await execute_tool(PHONE, 'financial_education', {'term': 'cotización'})
    assert result.service_invoked == 'assistant'
    render.assert_not_awaited()
