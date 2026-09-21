"""Preparation and in-memory rendering of movement charts."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from app.services.finance import CategoryMovementTotal


MAX_NAMED_CATEGORIES = 5
MAX_PNG_BYTES = 5 * 1024 * 1024
_RENDER_SLOTS = threading.BoundedSemaphore(value=2)


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
            labels = [item.category_name for item in spec.categories]
            values = [float(item.amount) for item in spec.categories]
            title_kind = "Ingresos" if spec.movement_type == "ingreso" else "Gastos"
            period = f"{spec.start_date:%d/%m/%Y} - {spec.end_date:%d/%m/%Y}"
            title = f"{title_kind} por categoría\n{period} | {spec.currency}"

            figure = Figure(figsize=(8, 6), dpi=120, constrained_layout=True)
            try:
                canvas = FigureCanvasAgg(figure)
                axis = figure.add_subplot(1, 1, 1)

                if spec.chart_type == "pie":
                    wedges, _, _ = axis.pie(
                        values,
                        autopct="%1.1f%%",
                        startangle=90,
                        pctdistance=0.72,
                        textprops={"fontsize": 9},
                    )
                    legend_labels = [
                        f"{label}: {MovementChartService._amount(value)}"
                        for label, value in zip(labels, values)
                    ]
                    axis.legend(
                        wedges,
                        legend_labels,
                        loc="lower center",
                        bbox_to_anchor=(0.5, -0.18),
                        ncol=2,
                        frameon=False,
                        fontsize=9,
                    )
                    axis.axis("equal")
                else:
                    positions = list(range(len(labels)))
                    bars = axis.barh(positions, values, color="#6C63FF")
                    axis.set_yticks(positions, labels=labels)
                    axis.invert_yaxis()
                    axis.set_xlabel(spec.currency)
                    axis.grid(axis="x", alpha=0.2)
                    axis.bar_label(
                        bars,
                        labels=[MovementChartService._amount(value) for value in values],
                        padding=4,
                        fontsize=9,
                    )
                    axis.spines[["top", "right", "left"]].set_visible(False)

                figure.suptitle(title, fontsize=15, fontweight="bold")
                figure.text(
                    0.5,
                    0.015,
                    f"Total: {MovementChartService._amount(spec.total)} {spec.currency}",
                    ha="center",
                    fontsize=11,
                    fontweight="bold",
                )

                buffer = io.BytesIO()
                canvas.print_png(buffer)
                png = buffer.getvalue()
            finally:
                figure.clear()
        if len(png) > MAX_PNG_BYTES:
            raise ValueError("Generated chart exceeds the configured size limit")
        return png

    @staticmethod
    def _amount(value: Decimal | float) -> str:
        numeric = Decimal(str(value)).quantize(Decimal("0.01"))
        rendered = f"{numeric:,.2f}"
        return rendered.replace(",", "_").replace(".", ",").replace("_", ".")
