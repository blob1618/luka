"""Chart orchestration kept out of the message dispatcher."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.api.whatsapp import WhatsAppImage
from app.services.chart_query import query_chart
from app.services.chart_request import (
    ChartRequest,
    chart_patch,
    month_label,
    normalized,
)
from app.services.conversation import ConversationService, PendingMovementChart
from app.services.finance import CategoryMovementTotal
from app.services.movement_chart import MovementChartService, MovementChartSpec

logger = logging.getLogger(__name__)
TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass
class ChartReply:
    text: str
    images: list[WhatsAppImage] = field(default_factory=list)


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


async def ask(phone, request, question, *, reason, options=None):
    pending = PendingMovementChart(
        sender_phone=phone,
        request=request,
        reason=reason,
        question=question,
        options=options or [],
    )
    await ConversationService.set_pending_movement_chart(phone, pending)
    return ChartReply(question)


def choice_patch(pending, text):
    value = normalized(text).strip(" .")
    for index, option in enumerate(pending.options, 1):
        if value in (str(index), f"opcion {index}", normalized(option["label"])):
            return option["patch"]
    return None


async def generate_chart(phone: str, data: dict, *, today=None) -> ChartReply:
    today = today or datetime.now(TZ).date()
    if data.get("chart_mode") == "unsupported":
        return ChartReply(
            "Puedo comparar meses completos, no días o semanas. Indicá dos meses y sus años."
        )
    try:
        request = ChartRequest.model_validate(chart_patch(data))
    except ValidationError:
        return await ask(
            phone,
            chart_patch(data),
            "Revisá los filtros: hasta cinco categorías en el ranking, seis categorías filtradas, y dos meses para comparar. Indicá qué querés corregir.",
            reason="filters",
        )
    if request.chart_mode == "movement_comparison":
        request.movement_type = "both"
    elif request.chart_mode == "category" and request.movement_type == "both":
        request.chart_mode = "movement_comparison"
    payload = request.model_dump(mode="json")
    try:
        periods = request.periods(today)
    except ValueError as exc:
        return await ask(phone, payload, str(exc), reason="period")
    payload = request.model_dump(mode="json")
    if request.chart_type == "pie" and (
        request.chart_mode != "category" or request.movement_type == "both"
    ):
        if request.chart_mode in ("monthly", "month_comparison"):
            options = [{"label": "Barras por mes", "patch": {"chart_type": "bar"}}]
            for month, start, end in periods[:2]:
                if request.movement_type == "both":
                    for kind, label in (("egreso", "gastos"), ("ingreso", "ingresos")):
                        options.append(
                            {
                                "label": f"Pastel de {label} de {month_label(month)}",
                                "patch": {
                                    "chart_type": "pie",
                                    "chart_mode": "category",
                                    "movement_type": kind,
                                    "date_from": start.isoformat(),
                                    "date_to": end.isoformat(),
                                },
                            }
                        )
                else:
                    options.append(
                        {
                            "label": f"Pastel de {month_label(month)}",
                            "patch": {
                                "chart_type": "pie",
                                "chart_mode": "category",
                                "date_from": start.isoformat(),
                                "date_to": end.isoformat(),
                            },
                        }
                    )
        else:
            options = [
                {"label": "Comparar en barras", "patch": {"chart_type": "bar"}},
                {
                    "label": "Pastel de gastos por categoría",
                    "patch": {"chart_mode": "category", "movement_type": "egreso"},
                },
                {
                    "label": "Pastel de ingresos por categoría",
                    "patch": {"chart_mode": "category", "movement_type": "ingreso"},
                },
            ]
        question = (
            "Para comparar usá barras; el pastel muestra una distribución. Elegí una opción:\n"
            + "\n".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1))
        )
        return await ask(
            phone, payload, question, reason="distribution", options=options
        )
    try:
        result = await asyncio.to_thread(query_chart, phone, request, periods)
        if result.status == "needs_currency":
            options = [
                {"label": c, "patch": {"chart_currency": c}} for c in result.options
            ]
            question = "Hay más de una moneda. Elegí una:\n" + "\n".join(
                f"{i}. {c}" for i, c in enumerate(result.options, 1)
            )
            return await ask(
                phone, payload, question, reason="currency", options=options
            )
        if result.status == "needs_categories":
            names = result.options[:6]
            options = [
                {"label": name, "patch": {"chart_categories": [name]}} for name in names
            ]
            question = (
                result.message
                + "\n"
                + "\n".join(f"{i}. {name}" for i, name in enumerate(names, 1))
            )
            return await ask(
                phone, payload, question, reason="categories", options=options
            )
        if result.status != "ok":
            return ChartReply(result.message)
        if not result.rows or not any(row.amount > 0 for row in result.rows):
            kind = (
                "ingresos"
                if request.movement_type == "ingreso"
                else "gastos"
                if request.movement_type == "egreso"
                else "movimientos"
            )
            await ConversationService.clear_state(phone)
            return ChartReply(f"No encontré {kind} para esos filtros y período.")
        payload["chart_currency"] = result.currency
        specs = prepare_charts(request, result, periods, today)
        images = []
        for spec in specs:
            png = await asyncio.wait_for(
                asyncio.to_thread(MovementChartService.render_png, spec), timeout=8
            )
            period = (
                spec.period_label
                or f"{spec.start_date:%d/%m/%Y} – {spec.end_date:%d/%m/%Y}"
            )
            caption = f"{spec.heading or ('Ingresos' if spec.movement_type == 'ingreso' else 'Gastos') + ' por categoría'} | {period} | {spec.currency}\n{spec.scope}\n{spec.summary or 'Total: ' + MovementChartService._amount(spec.total) + ' ' + spec.currency}"
            if spec.note:
                caption += "\n" + spec.note
            images.append(WhatsAppImage(content=png, caption=caption[:1024]))
        await ConversationService.clear_state(phone)
        await ConversationService.set_last_chart(phone, payload)
        return ChartReply(images[0].caption, images)
    except Exception:
        logger.warning("chart_generation_failed", exc_info=False)
        await ConversationService.clear_state(phone)
        return ChartReply(
            "No pude generar el gráfico. Podés volver a pedirlo en unos minutos."
        )
