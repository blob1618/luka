"""Preparation and in-memory rendering of movement charts."""

from __future__ import annotations

import io
import threading
import hashlib
import textwrap
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from app.services.finance import CategoryMovementTotal


MAX_NAMED_CATEGORIES = 5
MAX_PNG_BYTES = 5 * 1024 * 1024
_RENDER_SLOTS = threading.BoundedSemaphore(value=2)
PALETTE = ("#3066BE", "#087F8C", "#7A5195", "#BB5A24", "#A33757", "#58752D")
INK = "#172B4D"


def category_color(name: str) -> str:
    if name == "Otros":
        return "#7B8798"
    index = int(hashlib.sha256(name.casefold().encode()).hexdigest()[:8], 16)
    return PALETTE[index % len(PALETTE)]


def wrapped_label(name: str, width: int = 32) -> str:
    return "\n".join(textwrap.wrap(name, width=width) or [name])


def category_colors(labels: list[str]) -> list[str]:
    assigned = {}
    used = set()
    for label in sorted(set(labels), key=str.casefold):
        color = category_color(label)
        if label != "Otros" and color in used:
            color = next(
                (candidate for candidate in PALETTE if candidate not in used), color
            )
        assigned[label] = color
        used.add(color)
    return [assigned[label] for label in labels]


@dataclass(frozen=True)
class MovementChartSpec:
    categories: tuple[CategoryMovementTotal, ...]
    total: Decimal
    movement_type: str
    currency: str
    start_date: date
    end_date: date
    chart_type: str
    ranking: str
    heading: str | None = None
    scope: str = ""
    summary: str | None = None
    note: str = ""
    percentages: bool = True
    scale_max: Decimal | None = None
    colors: tuple[str, ...] = ()
    period_label: str | None = None


class MovementChartService:
    @staticmethod
    def prepare(
        categories: list[CategoryMovementTotal],
        *,
        movement_type: str,
        currency: str,
        start_date: date,
        end_date: date,
        chart_type: str = "bar",
        ranking: str = "highest",
        limit: int = MAX_NAMED_CATEGORIES,
        percentages: bool = True,
        scope: str = "",
    ) -> MovementChartSpec:
        normalized_chart = "pie" if chart_type == "pie" else "bar"
        normalized_ranking = "lowest" if ranking == "lowest" else "highest"
        ordered = sorted(
            categories,
            key=lambda item: (
                item.amount if normalized_ranking == "lowest" else -item.amount,
                item.category_name.casefold(),
            ),
        )
        total = sum((item.amount for item in categories), Decimal("0"))
        if not 1 <= limit <= MAX_NAMED_CATEGORIES:
            raise ValueError("Choose between one and five categories")
        visible = ordered[:limit]
        if len(ordered) > limit:
            visible_total = sum((item.amount for item in visible), Decimal("0"))
            visible.append(CategoryMovementTotal("Otros", total - visible_total))

        return MovementChartSpec(
            categories=tuple(visible),
            total=total,
            movement_type=movement_type,
            currency=currency,
            start_date=start_date,
            end_date=end_date,
            chart_type=normalized_chart,
            ranking=normalized_ranking,
            percentages=percentages,
            scope=scope,
        )

    @staticmethod
    def render_png(spec: MovementChartSpec) -> bytes:
        if not spec.categories or spec.total <= 0:
            raise ValueError("A chart needs at least one positive category")

        with _RENDER_SLOTS:
            figure = MovementChartService.build_figure(spec)
            try:
                canvas = FigureCanvasAgg(figure)
                buffer = io.BytesIO()
                canvas.print_png(buffer)
                png = buffer.getvalue()
            finally:
                figure.clear()
        if len(png) > MAX_PNG_BYTES:
            raise ValueError("Generated chart exceeds the configured size limit")
        return png

    @staticmethod
    def build_figure(spec: MovementChartSpec) -> Figure:
        """Use dedicated rows, not floating footers, to keep mobile labels clear."""
        labels = [item.category_name for item in spec.categories]
        values = [float(item.amount) for item in spec.categories]
        colors = list(spec.colors) or category_colors(labels)
        is_pie = spec.chart_type == "pie"
        label_width = 22 if is_pie else 32
        line_counts = [
            len(wrapped_label(label, label_width).splitlines()) for label in labels
        ]
        row_heights = [
            (0.5 if is_pie else 0.95) + 0.55 * (count - 1) for count in line_counts
        ]
        scope_text = wrapped_label(spec.scope, 48) if spec.scope else ""
        scope_height = 0.24 * len(scope_text.splitlines()) if scope_text else 0
        note_text = wrapped_label(spec.note, 48) if spec.note else ""
        heights = (
            [1.65]
            + ([scope_height] if scope_text else [])
            + ([2.8] if is_pie else [])
            + row_heights
            + [max(0.3, len(note_text.splitlines()) * 0.28)]
        )
        figure = Figure(figsize=(6.8, sum(heights)), dpi=160, facecolor="#FFFFFF")
        grid = figure.add_gridspec(
            len(heights),
            1,
            height_ratios=heights,
            left=0.07,
            right=0.93,
            bottom=0.025,
            top=0.975,
            hspace=0.12,
        )
        header = figure.add_subplot(grid[0])
        header.set_axis_off()
        kind = "Ingresos" if spec.movement_type == "ingreso" else "Gastos"
        ranking = (
            "Menores importes primero"
            if spec.ranking == "lowest"
            else "Mayores importes primero"
        )
        header.text(
            0,
            0.96,
            spec.heading or f"{kind} por categoría",
            va="top",
            fontsize=22,
            weight="bold",
            color=INK,
        )
        period = (
            spec.period_label
            or f"{spec.start_date:%d/%m/%Y} – {spec.end_date:%d/%m/%Y}"
        )
        header.text(
            0,
            0.69,
            f"{period} · {spec.currency}",
            va="top",
            fontsize=13,
            color="#526078",
        )
        header.text(
            0,
            0.40,
            spec.summary
            or f"Total  {MovementChartService._amount(spec.total)} {spec.currency}",
            va="top",
            fontsize=19,
            weight="bold",
            color=INK,
        )
        header.text(
            0,
            0.12,
            ranking if not spec.heading else "Importes registrados · " + spec.currency,
            va="top",
            fontsize=12,
            color="#526078",
        )
        offset = 1
        if scope_text:
            scope_axis = figure.add_subplot(grid[offset])
            scope_axis.set_axis_off()
            scope_axis.text(0, 1, scope_text, va="top", fontsize=12, color="#526078")
            offset += 1
        if is_pie:
            pie = figure.add_subplot(grid[offset])
            pie.pie(
                values,
                colors=colors,
                startangle=90,
                counterclock=False,
                wedgeprops={"linewidth": 2, "edgecolor": "white"},
            )
            pie.set_aspect("equal")
            offset += 1
        maximum = float(spec.scale_max) if spec.scale_max else max(values)
        maximum = maximum or 1
        for index, (label, item, value, color) in enumerate(
            zip(labels, spec.categories, values, colors)
        ):
            row = figure.add_subplot(grid[index + offset])
            row.set_axis_off()
            percentage = item.amount / spec.total * 100 if spec.total else Decimal(0)
            row.text(
                0.055 if is_pie else 0,
                0.95,
                wrapped_label(label, label_width),
                transform=row.transAxes,
                va="top",
                fontsize=17,
                weight="bold",
                color=INK,
            )
            amount = MovementChartService._amount(item.amount)
            percent_text = f"{percentage:.1f}".replace(".", ",")
            detail = f"{amount}  ·  {percent_text}%" if spec.percentages else amount
            row.text(
                1,
                0.95 if is_pie else 0.27,
                detail,
                transform=row.transAxes,
                va="top" if is_pie else "bottom",
                ha="right",
                fontsize=16,
                color=INK,
            )
            if is_pie:
                row.plot(
                    [0.01],
                    [0.70],
                    marker="o",
                    markersize=9,
                    transform=row.transAxes,
                    color=color,
                )
            else:
                row.set_xlim(0, maximum)
                row.set_ylim(0, 1)
                row.barh(0.10, maximum, height=0.15, color="#EEF2F7")
                row.barh(0.10, value, height=0.15, color=color)
        if note_text:
            note = figure.add_subplot(grid[-1])
            note.set_axis_off()
            note.text(0, 1, note_text, va="top", fontsize=12, color="#526078")
        return figure

    @staticmethod
    def _amount(value: Decimal | float) -> str:
        numeric = Decimal(str(value)).quantize(Decimal("0.01"))
        rendered = f"{numeric:,.2f}"
        localized = rendered.replace(",", "_").replace(".", ",").replace("_", ".")
        return localized.removesuffix(",00")
