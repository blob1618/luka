"""Read-only, currency-safe aggregation for category and monthly charts."""

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import and_, extract, func, or_

from app.models.database import Categoria, MovimientoFinanciero, SessionLocal, Usuario
from app.services.chart_request import ChartRequest, normalized


@dataclass(frozen=True)
class ChartValue:
    category: str
    month: str
    movement_type: str
    amount: Decimal


@dataclass
class ChartQueryResult:
    status: str
    rows: list[ChartValue] = field(default_factory=list)
    currency: str | None = None
    options: list[str] = field(default_factory=list)
    message: str = ""


def query_chart(
    phone: str, request: ChartRequest, periods: list, *, session=None
) -> ChartQueryResult:
    owned_session = session is None
    session = session or SessionLocal()
    try:
        user = session.query(Usuario.id).filter(Usuario.whatsapp_id == phone).first()
        if user is None:
            return ChartQueryResult(
                "error", message="No encontré una cuenta vinculada a este WhatsApp."
            )
        category_ids = []
        if request.chart_categories:
            available = (
                session.query(Categoria.id, Categoria.nombre)
                .filter(
                    Categoria.usuario_id == user.id,
                    Categoria.esta_eliminado.is_(False),
                )
                .all()
            )
            for name in request.chart_categories:
                if normalized(name) == "sin categoria":
                    category_ids.append(None)
                    continue
                matches = [item for item in available if item.nombre == name.strip()]
                if not matches:
                    matches = [
                        item
                        for item in available
                        if normalized(item.nombre) == normalized(name)
                    ]
                if len(matches) != 1:
                    return ChartQueryResult(
                        "needs_categories",
                        options=[item.nombre for item in available],
                        message=f"No pude identificar de forma única la categoría «{name}». Indicá los nombres completos.",
                    )
                category_ids.append(matches[0].id)
        movement = MovimientoFinanciero
        year, month = (
            extract("year", movement.fecha_movimiento),
            extract("month", movement.fecha_movimiento),
        )
        category = func.coalesce(Categoria.nombre, "Sin categoría")
        query = (
            session.query(
                category.label("category"),
                year.label("year"),
                month.label("month"),
                movement.tipo,
                movement.moneda,
                func.sum(movement.cantidad).label("amount"),
            )
            .outerjoin(
                Categoria,
                and_(
                    Categoria.id == movement.categoria_id,
                    Categoria.usuario_id == user.id,
                ),
            )
            .filter(
                movement.usuario_id == user.id,
                movement.anulado_en.is_(None),
                or_(
                    *(
                        and_(
                            movement.fecha_movimiento >= start,
                            movement.fecha_movimiento <= end,
                        )
                        for _, start, end in periods
                    )
                ),
            )
        )
        if request.movement_type != "both":
            query = query.filter(movement.tipo == request.movement_type)
        if request.chart_currency:
            query = query.filter(movement.moneda == request.chart_currency)
        if category_ids:
            predicate = movement.categoria_id.in_(
                [value for value in category_ids if value is not None]
            )
            if None in category_ids:
                predicate = or_(predicate, movement.categoria_id.is_(None))
            query = query.filter(predicate)
        rows = query.group_by(
            category, year, month, movement.tipo, movement.moneda
        ).all()
        currencies = sorted({row.moneda for row in rows})
        if not request.chart_currency and len(currencies) > 1:
            return ChartQueryResult("needs_currency", options=currencies)
        return ChartQueryResult(
            "ok",
            currency=request.chart_currency or (currencies[0] if currencies else None),
            rows=[
                ChartValue(
                    row.category,
                    f"{int(row.year):04}-{int(row.month):02}",
                    row.tipo,
                    Decimal(str(row.amount)),
                )
                for row in rows
            ],
        )
    finally:
        if owned_session:
            session.close()
