"""Read operations collect chart filters without legacy pending state."""
import asyncio

from pydantic import ConfigDict

from app.api.whatsapp import WhatsAppImage
from app.services import finance
from app.services.assistant_contracts import CommitResult, FieldRequest, IncompleteOperation, ReferenceToken, ResourceReference
from app.services.assistant_capabilities.common import issue_resource, operation_tool, prepared
from app.services.chart_request import ChartRequest
from app.services.chart_query import query_chart
from app.services.chart_service import prepare_charts
from app.services.movement_chart import MovementChartService


class ChartInput(ChartRequest):
    model_config = ConfigDict(extra='forbid')
    previous_ref: ReferenceToken | None = None


async def chart_request(context, operation):
    values = dict(operation.draft_payload)
    ref = values.get('previous_ref')
    if ref:
        previous = await context.references.resolve(context.turn, ref, kind='chart_query', payload_model=ResourceReference)
        explicit = {key: value for key, value in values.items() if key in operation.field_evidence}
        values = {**previous.snapshot, **explicit}
    request = ChartInput.model_validate(values)
    if request.chart_mode == 'movement_comparison':
        request.movement_type = 'both'
    elif request.chart_mode == 'category' and request.movement_type == 'both':
        request.chart_mode = 'movement_comparison'
    return request


async def choose(context, field, options):
    refs, choices = [], {}
    for label, patch in options:
        ref = await issue_resource(context, 'chart_choice', label, {'patch': patch}, {'label': label, 'patch': patch})
        refs.append(ref)
        choices[str(len(choices) + 1)] = {'ref': ref, 'label': label}
    context.state['selection_options'] = choices
    return IncompleteOperation(missing_fields=(FieldRequest(field=field, reason='ambiguous', candidate_refs=tuple(refs), question='Elegí una de estas opciones para el gráfico.'),))


async def prepare_chart(context, operation):
    request = await chart_request(context, operation)
    try:
        periods = request.periods(context.turn.reference_day)
    except ValueError as exc:
        context.event_key, context.event_variables = 'chart.period_required', {'question': str(exc)}
        return IncompleteOperation(missing_fields=(FieldRequest(field='period', reason='missing', question='¿De qué período querés ver el gráfico?'),))
    if request.chart_type == 'pie' and (request.chart_mode != 'category' or request.movement_type == 'both'):
        options = [('Comparar en barras', {'chart_type': 'bar'})]
        if request.chart_mode in {'monthly', 'month_comparison'}:
            for month, start, end in periods[:2]:
                for kind in (('egreso', 'ingreso') if request.movement_type == 'both' else (request.movement_type,)):
                    options.append((f'Pastel de {kind} de {month}', {'chart_type': 'pie', 'chart_mode': 'category',
                        'movement_type': kind, 'date_from': str(start), 'date_to': str(end)}))
        else:
            options.extend([('Pastel de gastos por categoría', {'chart_mode': 'category', 'movement_type': 'egreso'}),
                            ('Pastel de ingresos por categoría', {'chart_mode': 'category', 'movement_type': 'ingreso'})])
        context.event_key, context.event_variables = 'chart.distribution_choice', {'options': '\n'.join(label for label, _ in options)}
        return await choose(context, 'distribution', options)
    def query():
        with finance.SessionLocal() as session:
            return query_chart(context.turn.phone, request, periods, session=session)
    result = await asyncio.to_thread(query)
    if result.status in {'needs_currency', 'needs_categories'}:
        field = 'chart_currency' if result.status == 'needs_currency' else 'chart_categories'
        context.event_key = 'chart.currency_choice' if field == 'chart_currency' else 'chart.category_choice'
        context.event_variables = {'options': ', '.join(result.options), 'question': result.message}
        return await choose(context, field, [(value, {field: value if field == 'chart_currency' else [value]}) for value in result.options])
    if result.status not in {'ok', 'no_data'}:
        raise ValueError(result.message)
    # Persist only validated filters; rows/images are re-queried on completion.
    return prepared(ChartInput.model_validate(request.model_dump()), 'Generar gráfico con los filtros indicados.')


async def render_chart(context, operation, confirmation):
    request = ChartInput.model_validate(operation.prepared_payload)
    periods = request.periods(context.turn.reference_day)
    def generate():
        with finance.SessionLocal() as session:
            result = query_chart(context.turn.phone, request, periods, session=session)
        if result.status == 'no_data' or not result.rows:
            return []
        if result.status != 'ok':
            raise ValueError(result.message)
        return [(spec, MovementChartService.render_png(spec)) for spec in prepare_charts(request, result, periods, context.turn.reference_day)]
    images = await asyncio.to_thread(generate)
    caption = 'Gráfico de movimientos registrados; monedas separadas.'
    for index, (spec, content) in enumerate(images):
        text = f'{spec.heading or "Movimientos"}: {spec.currency}, {spec.start_date} al {spec.end_date}. {spec.note}'
        context.outbound.append(WhatsAppImage(content=content, filename=f'luka-{index + 1}.png', caption=text[:1024]))
    if not images:
        caption = 'No hay movimientos registrados para ese gráfico.'
    await issue_resource(context, 'chart_query', operation.operation_id, request.model_dump(mode='json'), request.model_dump(mode='json'))
    facts = context.presenter.add([caption])
    return CommitResult(replayed=False, data={'presentation': caption}, facts=facts,
        event_key='chart.completed' if images else 'chart.no_data',
        event_variables={'caption': caption} if images else {'message': caption})


def chart_contracts():
    return (operation_tool('movement_chart', 'Genera imágenes de gráficos (categoría, ingresos/gastos, evolución mensual o dos meses) ante pedidos de gráfico o visualización. '
        'No reemplazar por dashboard_link. Barras/torta, ranking y porcentajes. Si faltan filtros, conserva borrador para operation_amend/resume; al completar genera imágenes sin confirmar escritura.',
        ChartInput, ChartInput, prepare_chart, render_chart, read=True),)
