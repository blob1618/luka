"""Read-only aggregation of a user's own movements for the last closed month.

HU-EDU-02 reports the registered difference for the previous calendar month on
request. This module only computes the aggregate: it does not build replies,
dispatch intents, convert currencies or persist anything.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.database import MovimientoFinanciero, SessionLocal, Usuario


logger = logging.getLogger(__name__)
ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass(frozen=True)
class CurrencyBalance:
    """Registered totals for one currency, without cross-currency conversion."""

    currency: str
    income: Decimal
    expense: Decimal
    difference: Decimal
    movement_count: int


@dataclass
class MonthlyBalanceResult:
    """Typed outcome of the monthly query.

    Status is one of ``ok``, ``zero_difference``, ``no_data``,
    ``user_not_found`` or ``error`` so callers can tell an empty period apart
    from a period whose registered totals cancel out.
    """

    status: str
    period_start: date
    period_end: date
    currencies: list[CurrencyBalance] = field(default_factory=list)
    message: str = ""


def argentina_now(now: datetime | None) -> datetime:
    """Return ``now`` as an aware instant in the Argentina timezone."""
    if now is None:
        return datetime.now(ARGENTINA_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=ARGENTINA_TZ)
    return now.astimezone(ARGENTINA_TZ)


def previous_month_period(reference: date) -> tuple[date, date]:
    """Return the closed month before ``reference`` as ``[start, end)``.

    The end is the first day of the reference month, so it is exclusive. This
    handles a January reference (previous December, across the year change) and
    leap/non-leap February without special cases.
    """
    current_start = date(reference.year, reference.month, 1)
    previous_end = current_start - timedelta(days=1)
    return date(previous_end.year, previous_end.month, 1), current_start


def query_monthly_balance(
    phone: str, *, now: datetime | None = None, session: Session | None = None
) -> MonthlyBalanceResult:
    """Aggregate the closed month's active movements for a WhatsApp account.

    Only movements owned by the resolved user and with ``anulado_en`` NULL are
    summed, grouped by currency and movement type, using ``Decimal``. The
    caller may inject ``now`` (used to derive the period) and a ``session``.
    """
    reference = argentina_now(now)
    period_start, period_end = previous_month_period(reference.date())

    submitted_phone = phone.strip() if isinstance(phone, str) else ""
    if not submitted_phone:
        return MonthlyBalanceResult(
            status="user_not_found",
            period_start=period_start,
            period_end=period_end,
            message="No hay una cuenta vinculada a ese WhatsApp.",
        )

    owned_session = session is None
    session = session or SessionLocal()
    try:
        user = (
            session.query(Usuario.id)
            .filter(Usuario.whatsapp_id == submitted_phone)
            .first()
        )
        if user is None:
            return MonthlyBalanceResult(
                status="user_not_found",
                period_start=period_start,
                period_end=period_end,
                message="No hay una cuenta vinculada a ese WhatsApp.",
            )

        rows = (
            session.query(
                MovimientoFinanciero.moneda,
                MovimientoFinanciero.tipo,
                func.sum(MovimientoFinanciero.cantidad).label("total"),
                func.count(MovimientoFinanciero.id).label("count"),
            )
            .filter(
                MovimientoFinanciero.usuario_id == user.id,
                MovimientoFinanciero.anulado_en.is_(None),
                MovimientoFinanciero.fecha_movimiento >= period_start,
                MovimientoFinanciero.fecha_movimiento < period_end,
            )
            .group_by(MovimientoFinanciero.moneda, MovimientoFinanciero.tipo)
            .order_by(MovimientoFinanciero.moneda)
            .all()
        )

        totals: dict[str, dict[str, Decimal]] = {}
        counts: dict[str, int] = {}
        for currency, movement_type, total, count in rows:
            bucket = totals.setdefault(
                currency, {"ingreso": Decimal("0"), "egreso": Decimal("0")}
            )
            bucket[movement_type] += Decimal(str(total or 0))
            counts[currency] = counts.get(currency, 0) + int(count)

        balances = [
            CurrencyBalance(
                currency=currency,
                income=bucket["ingreso"],
                expense=bucket["egreso"],
                difference=bucket["ingreso"] - bucket["egreso"],
                movement_count=counts[currency],
            )
            for currency, bucket in sorted(totals.items())
        ]

        if not balances:
            return MonthlyBalanceResult(
                status="no_data",
                period_start=period_start,
                period_end=period_end,
                message="No hay movimientos registrados en el período.",
            )
        if all(balance.difference == 0 for balance in balances):
            return MonthlyBalanceResult(
                status="zero_difference",
                period_start=period_start,
                period_end=period_end,
                currencies=balances,
                message="Los movimientos registrados se compensan en el período.",
            )
        return MonthlyBalanceResult(
            status="ok",
            period_start=period_start,
            period_end=period_end,
            currencies=balances,
        )
    except Exception as exc:
        logger.error("monthly_balance_query_failed error=%s", type(exc).__name__)
        return MonthlyBalanceResult(
            status="error",
            period_start=period_start,
            period_end=period_end,
            message="No se pudo calcular el período.",
        )
    finally:
        if owned_session:
            session.close()
