"""Currency-separated complete period aggregates and reconciled contributions."""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func

from app.models.database import Categoria, MovimientoFinanciero
from app.services.assistant_contracts import amount_text


def periods(args, today):
    def month(value, fallback):
        parsed = date.fromisoformat(value + '-01') if value else fallback.replace(day=1)
        if parsed > today:
            raise ValueError('No se comparan meses futuros')
        return parsed

    current = month(args.current_month, today)
    baseline = month(args.baseline_month, current - timedelta(days=1))
    if current == baseline:
        raise ValueError('Elegí períodos diferentes')
    cutoff = today.day if (current.year, current.month) == (today.year, today.month) else calendar.monthrange(current.year, current.month)[1]
    def end(start):
        last = calendar.monthrange(start.year, start.month)[1]
        day = min(cutoff, last) if args.basis == 'month_to_date' else last
        if (start.year, start.month) == (today.year, today.month):
            day = min(day, today.day)
        return start.replace(day=day) + timedelta(days=1)
    return (current, end(current)), (baseline, end(baseline))


def compare_periods(session, owner_id, args, today):
    current, baseline = periods(args, today)
    data = {}
    for name, (start, end) in [('current', current), ('baseline', baseline)]:
        q = session.query(
            MovimientoFinanciero.moneda, MovimientoFinanciero.tipo,
            MovimientoFinanciero.categoria_id, Categoria.nombre,
            func.sum(MovimientoFinanciero.cantidad), func.count(MovimientoFinanciero.id),
        ).outerjoin(Categoria, (Categoria.id == MovimientoFinanciero.categoria_id) & (Categoria.usuario_id == owner_id)).filter(
            MovimientoFinanciero.usuario_id == owner_id,
            MovimientoFinanciero.anulado_en.is_(None),
            MovimientoFinanciero.fecha_movimiento >= start,
            MovimientoFinanciero.fecha_movimiento < end,
        )
        if args.currency:
            q = q.filter(MovimientoFinanciero.moneda == args.currency)
        data[name] = q.group_by(MovimientoFinanciero.moneda, MovimientoFinanciero.tipo,
                                MovimientoFinanciero.categoria_id, Categoria.nombre).all()
    currencies = sorted({row[0] for rows in data.values() for row in rows})
    output, blocks = [], []
    ranges = f"{current[0]:%d/%m/%Y} al {current[1]-timedelta(days=1):%d/%m/%Y} frente a {baseline[0]:%d/%m/%Y} al {baseline[1]-timedelta(days=1):%d/%m/%Y}"
    blocks.append(f'Comparación de movimientos registrados: {ranges}. No incluye finanzas no registradas.')
    if current[1] == today + timedelta(days=1):
        blocks.append('El mes actual es parcial hasta la fecha indicada.')
    if not currencies:
        blocks.append('No hay movimientos registrados en esos períodos; no es una consulta de saldo bancario.')
    for currency in currencies:
        totals, counts, categories = {}, {}, {}
        for name, rows in data.items():
            totals[name] = {'ingreso': Decimal(0), 'egreso': Decimal(0)}
            counts[name] = 0
            for mon, kind, cat_id, label, total, count in rows:
                if mon != currency:
                    continue
                value = Decimal(str(total))
                totals[name][kind] += value
                counts[name] += int(count)
                if kind == 'egreso':
                    key = str(cat_id) if cat_id else None
                    cat = categories.setdefault(key, {'category_id': key, 'label': label or 'Sin categoría', 'current': Decimal(0), 'baseline': Decimal(0)})
                    cat[name] += value
        delta = totals['current']['egreso'] - totals['baseline']['egreso']
        contributions = []
        for cat in categories.values():
            cat['delta'] = cat['current'] - cat['baseline']
            contributions.append(cat)
        contributions.sort(key=lambda c: (-c['delta'], c['label'], c['category_id'] or ''))
        assert sum((c['delta'] for c in contributions), Decimal(0)) == delta
        item = {'currency': currency, 'counts': counts,
                'totals': {k: {t: str(v) for t, v in values.items()} for k, values in totals.items()},
                'expense_delta': str(delta),
                'percentage': str(delta / totals['baseline']['egreso'] * 100) if totals['baseline']['egreso'] else None,
                'contributions': [{k: str(v) if isinstance(v, Decimal) else v for k, v in c.items()} for c in contributions]}
        output.append(item)
        for name, label in [('current', 'Actual'), ('baseline', 'Base')]:
            values = totals[name]
            if not counts[name]:
                blocks.append(f'{currency} — {label}: sin datos registrados en el período.')
            else:
                blocks.append(f"{currency} — {label}: ingresos {amount_text(values['ingreso'])}; gastos {amount_text(values['egreso'])}; diferencia registrada {amount_text(values['ingreso']-values['egreso'])}.")
        blocks.append(f'{currency} — Variación de gastos: {amount_text(delta)}. ' +
                      (f"Variación porcentual: {amount_text(item['percentage'])} %." if item['percentage'] is not None else 'Porcentaje no calculable: base cero o sin datos.'))
        if contributions:
            biggest = contributions[0] if contributions[0]['delta'] >= 0 else min(contributions, key=lambda c: c['delta'])
            label = 'mayor aporte al aumento' if biggest['delta'] > 0 else ('mayor reducción registrada' if biggest['delta'] < 0 else 'sin variación registrada')
            blocks.append(f"{currency} — Categoría con {label}: {biggest['label']}, {amount_text(biggest['delta'])}. Es una descomposición de registros, no una causa demostrada.")
    return {'status': 'ok' if currencies else 'no_data', 'currencies': output,
            'current_period': [x.isoformat() for x in current], 'baseline_period': [x.isoformat() for x in baseline]}, blocks
