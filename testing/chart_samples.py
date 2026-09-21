"""Reproducible chart samples; no LLM, database queries or network access."""
from datetime import date
from decimal import Decimal

from app.services.finance import CategoryMovementTotal
from app.services.movement_chart import MovementChartService


def sample_specs():
    cases = {
        "Ejemplo original": [("Ocio", "82000"), ("Comida", "55000"), ("Ropa", "34000"), ("Transporte", "22000")],
        "Nombres largos y seis elementos": [
            ("Supermercado y artículos de limpieza del hogar", "175500.50"),
            ("Restaurantes y comidas fuera de casa", "93500"),
            ("Transporte público y combustible", "78000"),
            ("Salud y medicamentos", "34000"), ("Entretenimiento", "15000"),
            ("Mascotas", "5500"), ("Regalos", "2500"),
        ],
        "Importes muy desiguales": [("Vivienda", "1250000"), ("Transporte", "12500"), ("Comida", "950.75")],
    }
    return {
        f"{name} · {kind}": MovementChartService.prepare(
            [CategoryMovementTotal(label, Decimal(amount)) for label, amount in rows],
            movement_type="egreso", currency="ARS", start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30), chart_type=kind,
        )
        for name, rows in cases.items() for kind in ("bar", "pie")
    }
