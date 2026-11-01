"""Protected financial presentation and educational/catalog reads."""
import asyncio

from app.services import finance
from app.services.assistant_contracts import EmptyInput, FrozenModel, amount_text
from app.services.assistant_capabilities.common import data, read_tool
from app.services.assistant_capabilities.financial_presenters import render_registered_balance
from app.services.currency import QuoteType, QuoteUnavailableError, get_usd_quotes
from app.services.dashboard_link import DashboardLinkDecision, DashboardLinkService, dashboard_link_message
from app.services.financial_education import FinancialEducationService
from app.services.investment_guidance import InvestmentGuidanceError, load_catalog, render_overview, render_topic
from app.services.llm import LLMService
from app.services.monthly_balance import query_monthly_balance
from app.services.net_worth import calculate_net_worth, NetWorthStatus


class QuotesInput(FrozenModel):
    quote_types: tuple[QuoteType, ...] = tuple(QuoteType)


class EducationInput(FrozenModel):
    term: str


class InvestmentInput(FrozenModel):
    topic_id: str | None = None


def exact_amount(value):
    if value is None:
        return 'no disponible'
    return f'{value:,.{max(0, -value.as_tuple().exponent)}f}'.translate(str.maketrans('.,', ',.'))


def fact(key, label, value):
    return {'key': key, 'label': label, 'value': str(value)}


def usd_quote_facts(values, types):
    facts = []
    for kind in types:
        value = values.get(kind)
        label = kind.value
        if value is None:
            facts.append(fact(kind.name + '_STATUS', label, 'cotización no disponible'))
            continue
        facts.extend([fact(kind.name + '_COMPRA', 'Compra ' + label, exact_amount(value.compra)),
            fact(kind.name + '_VENTA', 'Venta ' + label, exact_amount(value.venta)),
            fact(kind.name + '_AS_OF', 'Actualizado ' + label, value.as_of.isoformat())])
        if value.stale:
            facts.append(fact(kind.name + '_STALE', 'Aviso ' + label, 'cotización desactualizada'))
    if values:
        facts.extend([fact('SOURCE', 'Fuente', ', '.join(sorted({v.fuente for v in values.values()}))),
            fact('FETCHED_AT', 'Consultado', max(v.fetched_at for v in values.values()).isoformat())])
    return facts, [kind.value for kind in types]


async def quotes(context, args):
    types = list(dict.fromkeys(args.quote_types))
    try:
        values = await get_usd_quotes(types)
    except QuoteUnavailableError:
        values = {}
    facts, requested = usd_quote_facts(values, types)
    text = await LLMService.render_financial_reply('usd_quotes', facts, requested)
    result = data(context, [], [text])
    # The quote is an external report, not a backend-computed monetary balance.
    result = type(result).model_validate({**result.model_dump(mode='json'),
        'facts': [dict(f.model_dump(mode='json'), origin='external_reported') for f in result.facts]})
    return result


def net_worth_facts(result, types):
    if result.status == NetWorthStatus.NO_WALLETS:
        facts, requested = [fact('STATUS', 'Estado', 'sin billeteras registradas')], []
    else:
        facts = [fact('ARS_TOTAL', 'Saldos ARS', amount_text(result.ars_total)),
                 fact('USD_TOTAL', 'Saldos USD', amount_text(result.usd_total))]
        requested = [kind.value for kind in types] if result.usd_total else []
        for kind in types if result.usd_total else ():
            total = result.totals[kind]
            facts.append(fact(kind.name + '_TOTAL', kind.value, 'total no disponible' if total is None else amount_text(total) + ' ARS'))
            if kind in result.stale:
                facts.append(fact(kind.name + '_STALE', 'Aviso ' + kind.value, 'cotización desactualizada'))
    return facts, requested


async def net_worth(context, args):
    def balances(values=None):
        with finance.SessionLocal() as session:
            return calculate_net_worth(context.turn.owner_id, session, values)
    result = await asyncio.to_thread(balances)
    types = list(dict.fromkeys(args.quote_types))
    if result.usd_total:
        try:
            values = await get_usd_quotes(types)
        except QuoteUnavailableError:
            values = {}
        result = await asyncio.to_thread(balances, values)
    facts, requested = net_worth_facts(result, types)
    text = await LLMService.render_financial_reply('net_worth', facts, requested)
    return data(context, [], [text])


async def previous_balance(context, args):
    def query():
        with finance.SessionLocal() as session:
            return query_monthly_balance(context.turn.phone, now=context.turn.now, session=session)
    result = await asyncio.to_thread(query)
    return data(context, [], [render_registered_balance(result)], status=result.status)


async def education(context, args):
    result = FinancialEducationService.answer(args.term, requested_term=args.term)
    return data(context, [], [result.text], status=result.status)


async def investment(context, args):
    try:
        text = render_topic(args.topic_id) if args.topic_id else render_overview()
    except InvestmentGuidanceError as exc:
        raise ValueError(str(exc)) from exc
    return data(context, [{'topic_id': topic.id, 'title': topic.title} for topic in load_catalog().topics], [text])


async def dashboard(context, args):
    result = await asyncio.to_thread(DashboardLinkService.generate_or_reuse, context.turn.phone,
        session_factory=finance.SessionLocal, now=context.turn.now)
    if result.decision == DashboardLinkDecision.SEND_LINK:
        message = dashboard_link_message(result.login_url, result.link_ttl_minutes)
        context.outbound.append(message)
        context.event_key = 'dashboard.link.sent'
        context.event_variables = {'login_url': result.login_url, 'ttl_minutes': str(result.link_ttl_minutes)}
        return data(context, [], [message.body])
    texts = {DashboardLinkDecision.NOT_ELIGIBLE: 'La cuenta debe estar vinculada para acceder al dashboard.',
        DashboardLinkDecision.SUPPRESS_RESPONSE: 'Ya envié un enlace vigente. Revisá el mensaje anterior.',
        DashboardLinkDecision.ERROR: 'No pude generar el enlace. Intentá nuevamente.'}
    context.event_key = 'dashboard.link.not_eligible' if result.decision == DashboardLinkDecision.NOT_ELIGIBLE else 'dashboard.link.error'
    return data(context, [], [texts[result.decision]], status=result.decision.value)


def financial_read_contracts():
    return (
        read_tool('query_net_worth', 'Consulta patrimonio propio ARS/USD con cotizaciones seleccionadas, datos exactos y avisos de disponibilidad. No mezclar con simples cotizaciones.', QuotesInput, net_worth),
        read_tool('query_usd_quotes', 'Consulta compra/venta USD por tipo, fuente, actualización y staleness; preserva precisión exacta sin redondear gastos.', QuotesInput, quotes),
        read_tool('previous_month_balance', 'Consulta diferencia registrada por moneda del último mes cerrado. Distingue sin datos de diferencia cero.', EmptyInput, previous_balance),
        read_tool('financial_education', 'Explica un término explícito con el glosario verificado y ejemplo; no da tasas actuales.', EducationInput, education),
        read_tool('investment_guidance', 'Orientación educativa con fuentes, revisión, riesgos y límites. Omitir topic_id lista catálogo; no es recomendación personal.', InvestmentInput, investment),
        read_tool('dashboard_link', 'Emite/reutiliza enlace backend de acceso personal al dashboard según vinculación, cooldown y expiración. Usala sólo si piden dashboard, panel web o link; nunca para pedidos de gráfico.', EmptyInput, dashboard, terminal=True),
    )
