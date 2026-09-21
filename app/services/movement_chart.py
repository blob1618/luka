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
            color = next((candidate for candidate in PALETTE if candidate not in used), color)
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
        visible = ordered[:MAX_NAMED_CATEGORIES]
        if len(ordered) > MAX_NAMED_CATEGORIES:
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
        colors = category_colors(labels)
        line_counts = [len(wrapped_label(label).splitlines()) for label in labels]
        row_heights = [0.95 + 0.55 * (count - 1) for count in line_counts]
        is_pie = spec.chart_type == "pie"
        heights = [1.65] + ([3.8] if is_pie else []) + row_heights + [0.3]
        figure = Figure(figsize=(6.8, sum(heights)), dpi=160, facecolor="#FFFFFF")
        grid = figure.add_gridspec(
            len(heights), 1, height_ratios=heights,
            left=0.07, right=0.93, bottom=0.025, top=0.975, hspace=0.12,
        )
        header = figure.add_subplot(grid[0])
        header.set_axis_off()
        kind = "Ingresos" if spec.movement_type == "ingreso" else "Gastos"
        ranking = "Menores importes primero" if spec.ranking == "lowest" else "Mayores importes primero"
        header.text(0, 0.96, f"{kind} por categoría", va="top", fontsize=22, weight="bold", color=INK)
        header.text(0, 0.69, f"{spec.start_date:%d/%m/%Y} – {spec.end_date:%d/%m/%Y} · {spec.currency}", va="top", fontsize=13, color="#526078")
        header.text(0, 0.40, f"Total  {MovementChartService._amount(spec.total)} {spec.currency}", va="top", fontsize=19, weight="bold", color=INK)
        header.text(0, 0.12, ranking, va="top", fontsize=12, color="#526078")
        offset = 1
        if is_pie:
            pie = figure.add_subplot(grid[1])
            pie.pie(values, colors=colors, startangle=90, counterclock=False,
                    wedgeprops={"linewidth": 2, "edgecolor": "white"})
            pie.set_aspect("equal")
            offset = 2
        maximum = max(values)
        for index, (label, item, value, color) in enumerate(zip(labels, spec.categories, values, colors)):
            row = figure.add_subplot(grid[index + offset])
            row.set_axis_off()
            percentage = item.amount / spec.total * 100
            row.text(0, 0.95, wrapped_label(label), transform=row.transAxes,
                     va="top", fontsize=17, weight="bold", color=INK)
            amount = MovementChartService._amount(item.amount)
            percent_text = f"{percentage:.1f}".replace(".", ",")
            detail = f"{amount}  ·  {percent_text}%"
            row.text(1, 0.08 if is_pie else 0.27, detail, transform=row.transAxes,
                     va="bottom", ha="right", fontsize=16, color=INK)
            if is_pie:
                row.plot([0, 0.1], [0.20, 0.20], transform=row.transAxes,
                         linewidth=7, color=color, solid_capstyle="round")
            else:
                row.set_xlim(0, maximum)
                row.set_ylim(0, 1)
                row.barh(0.10, maximum, height=0.15, color="#EEF2F7")
                row.barh(0.10, value, height=0.15, color=color)
        return figure

    @staticmethod
    def _amount(value: Decimal | float) -> str:
        numeric = Decimal(str(value)).quantize(Decimal("0.01"))
        rendered = f"{numeric:,.2f}"
        localized = rendered.replace(",", "_").replace(".", ",").replace("_", ".")
        return localized.removesuffix(",00")
