"""Reproducible chart samples; no LLM, database queries or network access."""

from datetime import date
from decimal import Decimal

from app.services.finance import CategoryMovementTotal
from app.services.movement_chart import MovementChartService


def sample_specs():
    cases = {
        "Ejemplo original": [
            ("Ocio", "82000"),
            ("Comida", "55000"),
            ("Ropa", "34000"),
            ("Transporte", "22000"),
        ],
        "Nombres largos y seis elementos": [
            ("Supermercado y artículos de limpieza del hogar", "175500.50"),
            ("Restaurantes y comidas fuera de casa", "93500"),
            ("Transporte público y combustible", "78000"),
            ("Salud y medicamentos", "34000"),
            ("Entretenimiento", "15000"),
            ("Mascotas", "5500"),
            ("Regalos", "2500"),
        ],
        "Importes muy desiguales": [
            ("Vivienda", "1250000"),
            ("Transporte", "12500"),
            ("Comida", "950.75"),
        ],
    }
    samples = {
        f"{name} · {kind}": MovementChartService.prepare(
            [CategoryMovementTotal(label, Decimal(amount)) for label, amount in rows],
            movement_type="egreso",
            currency="ARS",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            chart_type=kind,
        )
        for name, rows in cases.items()
        for kind in ("bar", "pie")
    }
    from app.services.chart_query import ChartQueryResult, ChartValue
    from app.services.chart_request import ChartRequest
    from app.services.chart_service import prepare_charts

    today = date(2026, 9, 21)
    data = ChartQueryResult(
        "ok",
        [
            ChartValue("Ocio", "2026-08", "egreso", Decimal("70000")),
            ChartValue("Ocio", "2026-09", "egreso", Decimal("82000")),
            ChartValue("Ocio", "2026-09", "ingreso", Decimal("20000")),
        ],
        "ARS",
    )
    requests = {
        "Ingresos frente a gastos": ChartRequest(
            chart_mode="movement_comparison", movement_type="both"
        ),
        "Comparación de meses": ChartRequest(
            chart_mode="month_comparison",
            movement_type="both",
            chart_months=["2026-08", "2026-09"],
        ),
        "Evolución mensual": ChartRequest(
            chart_mode="monthly", chart_start_month="2026-01"
        ),
    }
    for name, request in requests.items():
        periods = request.periods(today)
        selected = ChartQueryResult(
            "ok",
            [
                row
                for row in data.rows
                if (
                    request.movement_type == "both"
                    or row.movement_type == request.movement_type
                )
                and any(
                    start.strftime("%Y-%m") <= row.month <= end.strftime("%Y-%m")
                    for _, start, end in periods
                )
            ],
            "ARS",
        )
        for index, spec in enumerate(
            prepare_charts(request, selected, periods, today), 1
        ):
            samples[f"{name} · parte {index}"] = spec
    return samples
