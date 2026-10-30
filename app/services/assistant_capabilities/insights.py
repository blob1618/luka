"""Comparison adapter issues reusable inclusive scopes from canonical aggregates."""
import asyncio
from datetime import date, timedelta

from pydantic import JsonValue

from app.services import finance
from app.services.assistant_contracts import ComparePeriods, DataResult, DatePeriod, EntityReference, FrozenModel, PeriodReference, ScopePayload
from app.services.assistant_registry import ToolContract, ToolPolicy
from app.services.period_insights import compare_periods


class ComparisonOutput(FrozenModel):
    status: str
    currencies: list[dict[str, JsonValue]]
    current_period: DatePeriod
    baseline_period: DatePeriod


def _compare(turn, args):
    with finance.SessionLocal() as session:
        return compare_periods(session, turn.owner_id, args, turn.reference_day)


async def compare(context, args):
    data, lines = await asyncio.to_thread(_compare, context.turn, args)
    periods = {}
    for name in ('current', 'baseline'):
        start, end = data[name + '_period']
        periods[name] = DatePeriod(start=date.fromisoformat(start), end=date.fromisoformat(end) - timedelta(days=1))
        data[name + '_period'] = periods[name].model_dump(mode='json')
    for currency in data['currencies']:
        for name in ('current', 'baseline'):
            scope = ScopePayload(period=periods[name], currency=currency['currency'], movement_type='egreso')
            ref = await context.reference('query_scope', scope)
            currency[name] = PeriodReference(ref=ref, period=scope.period, currency=scope.currency,
                movement_type=scope.movement_type, label=name + ' ' + scope.currency).model_dump(mode='json')
        for contribution in currency['contributions']:
            contribution['category_ref'] = await context.reference('category', EntityReference(
                id=contribution.pop('category_id'), label=contribution['label']))
    return DataResult(data=ComparisonOutput.model_validate(data).model_dump(mode='json'), facts=context.presenter.add(lines))


def insight_contracts():
    return (ToolContract('compare_periods', 'Compara registros completos por moneda. month_to_date mantiene el mismo corte '
        'en ambos meses; full_months sólo si se piden meses completos. Devuelve current/baseline con ref de scope '
        'para buscar detalles sin reconstruir fechas ni mezclar monedas. Sólo descompone por categoría: no identifica '
        'movimientos individuales. Si el pedido incluye un gasto puntual, consultá find_movements usando el scope '
        'devuelto y, si corresponde, category_ref; para el mayor gasto usá sort_by amount_desc. '
        'Las contribuciones son registros, no causas.',
        ComparePeriods, ComparisonOutput, compare, ToolPolicy('read')),)
