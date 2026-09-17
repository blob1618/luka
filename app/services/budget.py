"""Budget consumption and availability derived from persisted movements."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func

from app.models.database import (
    Categoria,
    LimiteCategoria,
    MovimientoFinanciero,
    SessionLocal,
)
from app.services.categories_taxonomy import resolve_category_for_user


ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass(frozen=True)
class BudgetStatus:
    limit_id: str
    user_id: str
    category_id: str
    category_name: str
    currency: str
    period_start: date
    period_end: date
    limit_amount: Decimal
    spent_amount: Decimal
    remaining_amount: Decimal
    exceeded_amount: Decimal
    percentage: Decimal
    state: str


@dataclass
class BudgetStatusResult:
    status: str
    message: str
    budget: BudgetStatus | None = None


@dataclass
class BudgetListResult:
    status: str
    message: str
    budgets: list[BudgetStatus] = field(default_factory=list)


@dataclass
class BudgetEvaluation:
    status: str
    message: str
    movement_id: str | None = None
    has_limit: bool = False
    should_alert: bool = False
    budget: BudgetStatus | None = None


class BudgetService:
    """Calculates budgets without persisting derived consumed/remaining totals."""

    @staticmethod
    def _uuid(value: Any) -> UUID | None:
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _currency(value: Any) -> str | None:
        if value is None:
            return None
        currency = str(value).strip().upper()
        return currency if len(currency) == 3 and currency.isalpha() else None

    @staticmethod
    def _today() -> date:
        return datetime.now(ARGENTINA_TZ).date()

    @staticmethod
    def _active_category_names(session, user_id: Any) -> set[str]:
        rows = (
            session.query(Categoria.nombre)
            .filter(
                Categoria.usuario_id == user_id,
                Categoria.esta_eliminado.is_(False),
            )
            .all()
        )
        return {row[0] for row in rows}

    @classmethod
    def _resolve_category_id(cls, session, user_id: Any, category_name: str):
        resolved = resolve_category_for_user(
            category_name,
            cls._active_category_names(session, user_id),
        )
        if resolved is None:
            return None
        normalized = resolved.strip().lower()
        return (
            session.query(Categoria.id)
            .filter(
                Categoria.usuario_id == user_id,
                Categoria.esta_eliminado.is_(False),
                func.lower(func.trim(Categoria.nombre)) == normalized,
            )
            .scalar()
        )

    @staticmethod
    def _build_status(row) -> BudgetStatus:
        (
            limit_id,
            user_id,
            category_id,
            category_name,
            currency,
            period_start,
            period_end,
            limit_amount,
            spent_amount,
        ) = row
        limit_amount = Decimal(str(limit_amount))
        spent_amount = Decimal(str(spent_amount or 0))
        difference = limit_amount - spent_amount
        remaining = max(difference, Decimal("0"))
        exceeded = max(-difference, Decimal("0"))
        percentage = (
            (spent_amount / limit_amount) * Decimal("100")
            if limit_amount > 0
            else Decimal("0")
        ).quantize(Decimal("0.1"))
        if spent_amount > limit_amount:
            state = "exceeded"
        elif spent_amount == limit_amount:
            state = "reached"
        else:
            state = "available"
        return BudgetStatus(
            limit_id=str(limit_id),
            user_id=str(user_id),
            category_id=str(category_id),
            category_name=category_name,
            currency=currency,
            period_start=period_start,
            period_end=period_end,
            limit_amount=limit_amount,
            spent_amount=spent_amount,
            remaining_amount=remaining,
            exceeded_amount=exceeded,
            percentage=percentage,
            state=state,
        )

    @classmethod
    def _query_statuses(
        cls,
        session,
        user_id: Any,
        reference_date: date,
        currency: str | None = None,
        category_id: Any | None = None,
    ) -> list[BudgetStatus]:
        spent = func.coalesce(func.sum(MovimientoFinanciero.cantidad), 0)
        query = (
            session.query(
                LimiteCategoria.id,
                LimiteCategoria.usuario_id,
                LimiteCategoria.categoria_id,
                Categoria.nombre,
                LimiteCategoria.moneda,
                LimiteCategoria.inicio_periodo,
                LimiteCategoria.fin_periodo,
                LimiteCategoria.cantidad_max,
                spent,
            )
            .join(Categoria, LimiteCategoria.categoria_id == Categoria.id)
            .outerjoin(
                MovimientoFinanciero,
                and_(
                    MovimientoFinanciero.usuario_id == LimiteCategoria.usuario_id,
                    MovimientoFinanciero.categoria_id == LimiteCategoria.categoria_id,
                    MovimientoFinanciero.tipo == "egreso",
                    MovimientoFinanciero.anulado_en.is_(None),
                    MovimientoFinanciero.moneda == LimiteCategoria.moneda,
                    MovimientoFinanciero.fecha_movimiento
                    >= LimiteCategoria.inicio_periodo,
                    MovimientoFinanciero.fecha_movimiento
                    <= LimiteCategoria.fin_periodo,
                ),
            )
            .filter(
                LimiteCategoria.usuario_id == user_id,
                LimiteCategoria.inicio_periodo <= reference_date,
                LimiteCategoria.fin_periodo >= reference_date,
                Categoria.esta_eliminado.is_(False),
            )
        )
        if currency is not None:
            query = query.filter(LimiteCategoria.moneda == currency)
        if category_id is not None:
            query = query.filter(LimiteCategoria.categoria_id == category_id)
        rows = (
            query.group_by(
                LimiteCategoria.id,
                LimiteCategoria.usuario_id,
                LimiteCategoria.categoria_id,
                Categoria.nombre,
                LimiteCategoria.moneda,
                LimiteCategoria.inicio_periodo,
                LimiteCategoria.fin_periodo,
                LimiteCategoria.cantidad_max,
            )
            .order_by(Categoria.nombre, LimiteCategoria.moneda)
            .all()
        )
        return [cls._build_status(row) for row in rows]

    @classmethod
    def get_status(
        cls,
        user_id: Any,
        category_name: str,
        reference_date: date | None = None,
        currency: str = "ARS",
    ) -> BudgetStatusResult:
        reference_date = reference_date or cls._today()
        normalized_currency = cls._currency(currency)
        if not category_name or normalized_currency is None:
            return BudgetStatusResult("invalid_data", "category and currency are required")
        session = SessionLocal()
        try:
            category_id = cls._resolve_category_id(session, user_id, category_name)
            if category_id is None:
                return BudgetStatusResult("category_not_found", "category not found")
            budgets = cls._query_statuses(
                session,
                user_id,
                reference_date,
                currency=normalized_currency,
                category_id=category_id,
            )
            if not budgets:
                return BudgetStatusResult("not_found", "budget not found")
            return BudgetStatusResult("ok", "budget found", budgets[0])
        except Exception as exc:
            print(f"[BUDGET_STATUS] Error: {type(exc).__name__}: {exc}")
            return BudgetStatusResult("error", "could not calculate budget")
        finally:
            session.close()

    @classmethod
    def list_statuses(
        cls,
        user_id: Any,
        reference_date: date | None = None,
        currency: str | None = None,
    ) -> BudgetListResult:
        reference_date = reference_date or cls._today()
        normalized_currency = cls._currency(currency) if currency is not None else None
        if currency is not None and normalized_currency is None:
            return BudgetListResult("invalid_data", "invalid currency")
        session = SessionLocal()
        try:
            budgets = cls._query_statuses(
                session,
                user_id,
                reference_date,
                currency=normalized_currency,
            )
            return BudgetListResult("ok", "budgets calculated", budgets)
        except Exception as exc:
            print(f"[BUDGET_LIST] Error: {type(exc).__name__}: {exc}")
            return BudgetListResult("error", "could not calculate budgets")
        finally:
            session.close()

    @classmethod
    def get_status_for_limit(cls, limit_id: Any) -> BudgetStatusResult:
        parsed_id = cls._uuid(limit_id)
        if parsed_id is None:
            return BudgetStatusResult("invalid_data", "invalid limit id")
        session = SessionLocal()
        try:
            limit_row = (
                session.query(LimiteCategoria)
                .filter(LimiteCategoria.id == parsed_id)
                .first()
            )
            if limit_row is None:
                return BudgetStatusResult("not_found", "budget not found")
            budgets = cls._query_statuses(
                session,
                limit_row.usuario_id,
                limit_row.inicio_periodo,
                currency=limit_row.moneda,
                category_id=limit_row.categoria_id,
            )
            if not budgets:
                return BudgetStatusResult("not_found", "budget not found")
            return BudgetStatusResult("ok", "budget found", budgets[0])
        except Exception as exc:
            print(f"[BUDGET_LIMIT_STATUS] Error: {type(exc).__name__}: {exc}")
            return BudgetStatusResult("error", "could not calculate budget")
        finally:
            session.close()

    @classmethod
    def evaluate_movement(cls, movement_id: Any) -> BudgetEvaluation:
        evaluations = cls.evaluate_movements([movement_id])
        if evaluations:
            return evaluations[0]
        return BudgetEvaluation("not_found", "movement not found")

    @classmethod
    def evaluate_movements(cls, movement_ids: list[Any]) -> list[BudgetEvaluation]:
        parsed_ids = [value for value in (cls._uuid(v) for v in movement_ids) if value]
        if not parsed_ids:
            return []
        session = SessionLocal()
        try:
            movements = (
                session.query(MovimientoFinanciero)
                .filter(MovimientoFinanciero.id.in_(parsed_ids))
                .filter(MovimientoFinanciero.anulado_en.is_(None))
                .all()
            )
            by_id = {str(movement.id): movement for movement in movements}
            grouped: dict[tuple[Any, date, str], list[MovimientoFinanciero]] = {}
            evaluations: dict[str, BudgetEvaluation] = {}
            for raw_id in parsed_ids:
                movement = by_id.get(str(raw_id))
                if movement is None:
                    evaluations[str(raw_id)] = BudgetEvaluation(
                        "not_found", "movement not found", movement_id=str(raw_id)
                    )
                    continue
                if movement.tipo != "egreso" or movement.categoria_id is None:
                    evaluations[str(raw_id)] = BudgetEvaluation(
                        "not_applicable",
                        "movement does not consume a category budget",
                        movement_id=str(raw_id),
                    )
                    continue
                key = (
                    movement.usuario_id,
                    movement.fecha_movimiento,
                    movement.moneda.upper(),
                )
                grouped.setdefault(key, []).append(movement)

            for (user_id, movement_date, currency), group in grouped.items():
                statuses = cls._query_statuses(
                    session,
                    user_id,
                    movement_date,
                    currency=currency,
                )
                status_by_category = {s.category_id: s for s in statuses}
                for movement in group:
                    budget = status_by_category.get(str(movement.categoria_id))
                    evaluations[str(movement.id)] = BudgetEvaluation(
                        status="ok" if budget is not None else "no_limit",
                        message="budget evaluated" if budget else "no matching budget",
                        movement_id=str(movement.id),
                        has_limit=budget is not None,
                        should_alert=budget is not None and budget.state == "exceeded",
                        budget=budget,
                    )

            return [evaluations[str(value)] for value in parsed_ids]
        except Exception as exc:
            print(f"[BUDGET_EVALUATION] Error: {type(exc).__name__}: {exc}")
            return [
                BudgetEvaluation(
                    "error",
                    "could not evaluate movement",
                    movement_id=str(value),
                )
                for value in parsed_ids
            ]
        finally:
            session.close()
