"""Pure chart specification preparation shared by the chart capability."""
from decimal import Decimal
from app.services.chart_request import month_label
from app.services.finance import CategoryMovementTotal
from app.services.movement_chart import MovementChartService, MovementChartSpec


def prepare_charts(request, result, periods, today) -> list[MovementChartSpec]:
    currency = result.currency
    scope = (
        "Categorías: " + ", ".join(request.chart_categories)
        if request.chart_categories
        else "Todas las categorías"
    )
    if request.chart_mode == "category" and request.movement_type != "both":
        totals = {}
        for row in result.rows:
            totals[row.category] = totals.get(row.category, Decimal(0)) + row.amount
        return [
            MovementChartService.prepare(
                [
                    CategoryMovementTotal(name, amount)
                    for name, amount in totals.items()
                ],
                movement_type=request.movement_type,
                currency=currency,
                start_date=periods[0][1],
                end_date=periods[-1][2],
                chart_type=request.chart_type,
                ranking=request.chart_ranking,
                limit=request.chart_limit,
                percentages=request.chart_percentages,
                scope=scope,
            )
        ]
    types = (
        ("ingreso", "egreso")
        if request.movement_type == "both"
        else (request.movement_type,)
    )
    labels = {"ingreso": "Ingresos", "egreso": "Gastos"}
    colors = {"ingreso": "#087F8C", "egreso": "#BB5A24"}
    values = []
    for month, _, _ in periods:
        for kind in types:
            amount = sum(
                (
                    row.amount
                    for row in result.rows
                    if row.movement_type == kind and (not month or row.month == month)
                ),
                Decimal(0),
            )
            label = month_label(month) if month else labels[kind]
            if month and len(types) == 2:
                label += " · " + labels[kind]
            values.append((CategoryMovementTotal(label, amount), colors[kind]))
    total = sum((item.amount for item, _ in values), Decimal(0))
    maximum = max(item.amount for item, _ in values)
    summary = None
    note = "0 = sin movimientos registrados."
    if len(types) == 2:
        income = sum(
            (row.amount for row in result.rows if row.movement_type == "ingreso"),
            Decimal(0),
        )
        expense = sum(
            (row.amount for row in result.rows if row.movement_type == "egreso"),
            Decimal(0),
        )
        summary = f"Saldo  {MovementChartService._amount(income - expense)} {currency}"
        note += " Saldo = ingresos − gastos."
    if request.chart_mode == "month_comparison":
        for kind in types:
            amounts = [
                sum(
                    (
                        r.amount
                        for r in result.rows
                        if r.month == month and r.movement_type == kind
                    ),
                    Decimal(0),
                )
                for month, _, _ in periods
            ]
            difference = amounts[1] - amounts[0]
            percentage = (
                f"{difference / amounts[0] * 100:+.1f}%".replace(".", ",")
                if amounts[0]
                else "porcentaje no calculable (base cero)"
            )
            note += f" {labels[kind]}: variación {MovementChartService._amount(difference)} {currency} ({percentage})."
    if any(
        start.year == today.year and start.month == today.month
        for _, start, _ in periods
    ):
        note += f" Mes actual parcial hasta {today:%d/%m/%Y}."
    heading = {
        "movement_comparison": "Ingresos y gastos",
        "monthly": "Evolución mensual",
        "month_comparison": "Comparación de meses",
        "category": "Ingresos y gastos",
    }[request.chart_mode]
    specs = []
    for index in range(0, len(values), 6):
        page = values[index : index + 6]
        page_scope = scope
        if len(values) > 6:
            page_scope += (
                f" · Parte {index // 6 + 1}/{(len(values) + 5) // 6} · misma escala"
            )
        specs.append(
            MovementChartSpec(
                categories=tuple(item for item, _ in page),
                total=total,
                movement_type=request.movement_type,
                currency=currency,
                start_date=periods[0][1],
                end_date=periods[-1][2],
                chart_type="bar",
                ranking="highest",
                heading=heading,
                scope=page_scope,
                summary=summary,
                note=note,
                percentages=False,
                scale_max=maximum,
                colors=tuple(color for _, color in page),
                period_label=(
                    " vs. ".join(month_label(month) for month, _, _ in periods)
                    if request.chart_mode == "month_comparison"
                    else None
                ),
            )
        )
    return specs
