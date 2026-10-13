"""Editable chart conversation stages; chart decisions remain in the backend."""

CHART_JOURNEY = "chart.journey"
CHART_STAGES = {
    "chart.period_required": "Aclarar período",
    "chart.distribution_choice": "Elegir barras o pastel",
    "chart.currency_choice": "Elegir moneda",
    "chart.category_choice": "Elegir categoría",
    "chart.invalid_filters": "Corregir filtros",
    "chart.completed": "Gráfico enviado",
    "chart.no_data": "Sin movimientos",
    "chart.failed": "No se pudo generar",
    "chart.cancelled": "Pedido cancelado",
}

CHART_TEXT_RESPONSES = {
    "chart.period_required": [{
        "id": "chart-period", "label": "Indica el período",
        "example": "En octubre", "outcomes": [
            {"event": "chart.completed", "label": "Período válido y gráfico generado"},
            {"event": "chart.distribution_choice", "label": "Hace falta elegir el tipo"},
            {"event": "chart.period_required", "label": "Período incompleto"},
        ],
    }],
    "chart.distribution_choice": [{
        "id": "chart-distribution", "label": "Elige una opción",
        "example": "2. Pastel de Oct 2026", "outcomes": [
            {"event": "chart.completed", "label": "Gráfico generado"},
            {"event": "chart.currency_choice", "label": "Hace falta elegir moneda"},
        ],
    }],
    "chart.currency_choice": [{
        "id": "chart-currency", "label": "Elige moneda",
        "example": "ARS", "outcomes": [
            {"event": "chart.completed", "label": "Gráfico generado"},
            {"event": "chart.category_choice", "label": "Hace falta elegir categoría"},
        ],
    }],
    "chart.category_choice": [{
        "id": "chart-category", "label": "Elige categoría",
        "example": "1", "outcomes": [
            {"event": "chart.completed", "label": "Gráfico generado"},
            {"event": "chart.no_data", "label": "Sin datos para ese filtro"},
        ],
    }],
    "chart.invalid_filters": [{
        "id": "chart-filters", "label": "Corrige los filtros",
        "example": "Solo ocio", "outcomes": [
            {"event": "chart.completed", "label": "Gráfico generado"},
            {"event": "chart.invalid_filters", "label": "Filtros aún inválidos"},
        ],
    }],
}


def chart_journey_definition() -> dict:
    bodies = {
        "chart.period_required": "{question}",
        "chart.distribution_choice": "Para comparar usá barras; el pastel muestra una distribución. Elegí una opción:\n{options}",
        "chart.currency_choice": "Hay más de una moneda. Elegí una:\n{options}",
        "chart.category_choice": "{question}\n{options}",
        "chart.invalid_filters": "{question}",
        "chart.completed": "{caption}",
        "chart.no_data": "{message}",
        "chart.failed": "{message}",
        "chart.cancelled": "Listo, cancelé el gráfico.",
    }
    return {
        "start_node": "period_required",
        "nodes": [
            {"id": event.split(".")[1], "type": "text", "body": body, "terminal": True}
            for event, body in bodies.items()
        ],
        "event_nodes": {event: event.split(".")[1] for event in CHART_STAGES},
        "response_examples": {},
    }
