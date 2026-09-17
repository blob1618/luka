import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.models.database import Categoria, LimiteCategoria, SessionLocal, Usuario
from app.services.categories_taxonomy import resolve_category_for_user

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
MAX_LIMIT_AMOUNT = Decimal("9999999999.99")

_MESES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


@dataclass
class LimitResult:
    """Resultado de una operación sobre límites de gasto por categoría."""
    status: str
    message: str
    limit_id: str | None = None
    category_name: str | None = None
    amount: Decimal | None = None
    month: int | None = None
    year: int | None = None
    currency: str | None = None
    proposed_month: int | None = None
    proposed_year: int | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LimitEntry:
    category_name: str
    amount: Decimal
    month: int
    year: int
    currency: str = "ARS"
    id: str | None = None


@dataclass
class LimitBatchResult:
    status: str
    deleted: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LimitListResult:
    status: str
    message: str
    limits: list[LimitEntry] = field(default_factory=list)


class LimitService:
    @staticmethod
    def _normalize_text(value: Any, max_length: int = 200) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:max_length]

    @staticmethod
    def _normalize_category_name(value: Any) -> str | None:
        """Normaliza un nombre visible sin limitarlo a la taxonomía base."""
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value)).strip(" \t\r\n.,;:!?¡¿")
        if not text or len(text) > 100 or not any(char.isalpha() for char in text):
            return None
        if any(ord(char) < 32 for char in text):
            return None
        return text

    @staticmethod
    def _normalize_amount(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not amount.is_finite() or amount <= 0 or amount > MAX_LIMIT_AMOUNT:
            return None
        return amount.quantize(Decimal("0.01"))

    @staticmethod
    def _normalize_currency(value: Any) -> str | None:
        if value is None:
            return "ARS"
        currency = str(value).strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            return None
        return currency

    @staticmethod
    def _normalize_month(value: Any) -> int | None:
        if value is None:
            return None
        try:
            month = int(value)
        except (TypeError, ValueError):
            return None
        if month < 1 or month > 12:
            return None
        return month

    @staticmethod
    def _normalize_year(value: Any) -> int | None:
        if value is None:
            return None
        try:
            year = int(value)
        except (TypeError, ValueError):
            return None
        return year if 1 <= year <= 9999 else None

    @classmethod
    def _result(
        cls,
        status: str,
        message: str,
        limit_id: str | None = None,
        category_name: str | None = None,
        amount: Decimal | None = None,
        month: int | None = None,
        year: int | None = None,
        currency: str | None = None,
        proposed_month: int | None = None,
        proposed_year: int | None = None,
        candidates: list[dict[str, Any]] | None = None,
    ) -> LimitResult:
        return LimitResult(
            status=status,
            message=message,
            limit_id=limit_id,
            category_name=category_name,
            amount=amount,
            month=month,
            year=year,
            currency=currency,
            proposed_month=proposed_month,
            proposed_year=proposed_year,
            candidates=candidates or [],
        )

    @staticmethod
    def _get_user(session, sender_phone: str) -> Usuario | None:
        return (
            session.query(Usuario)
            .filter(Usuario.whatsapp_id == sender_phone)
            .first()
        )

    @staticmethod
    def _active_category_names(session, user_id: Any) -> set[str]:
        return {
            row[0]
            for row in (
                session.query(Categoria.nombre)
                .filter(
                    Categoria.usuario_id == user_id,
                    Categoria.esta_eliminado.is_(False),
                )
                .all()
            )
        }

    @staticmethod
    def _find_category(session, user_id: Any, category_name: str, *, active=True):
        query = session.query(Categoria).filter(
            Categoria.usuario_id == user_id,
            func.lower(func.trim(Categoria.nombre)) == category_name.strip().lower(),
        )
        if active is not None:
            query = query.filter(Categoria.esta_eliminado.is_(not active))
        return query.first()

    @classmethod
    def _resolve_category(
        cls,
        session,
        user_id: Any,
        category_name: str,
        *,
        allow_creation: bool,
    ) -> tuple[Categoria | None, str, str | None]:
        requested = cls._normalize_category_name(category_name)
        if requested is None:
            return None, category_name, "invalid_category"

        resolved = resolve_category_for_user(
            requested,
            cls._active_category_names(session, user_id),
        )
        proposed_name = resolved or requested
        category = cls._find_category(session, user_id, proposed_name)
        if category is not None:
            return category, category.nombre, None
        if not allow_creation:
            return None, proposed_name, "needs_category_confirmation"

        deleted = cls._find_category(session, user_id, proposed_name, active=False)
        if deleted is not None:
            deleted.esta_eliminado = False
            return deleted, deleted.nombre, None
        category = Categoria(
            usuario_id=user_id,
            nombre=proposed_name,
            es_default=False,
            esta_eliminado=False,
        )
        session.add(category)
        session.flush()
        return category, category.nombre, None

    @staticmethod
    def _uuid(value: Any) -> UUID | None:
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    @classmethod
    def month_label(cls, month: int, year: int, current_year: int) -> str:
        name = _MESES[month] if 1 <= month <= 12 else ""
        if year != current_year:
            return f"{name} {year}"
        return name

    @classmethod
    def _resolve_period(
        cls,
        month: int | None,
        year: int | None,
        today: date,
    ) -> tuple[int, int, bool]:
        """Resuelve (year, month, needs_year_confirmation).

        - Con año explícito, se usa tal cual.
        - Sin mes, se usa el mes actual.
        - Con mes pero sin año: si el mes ya pasó en el año actual,
          propone el año siguiente (needs_year_confirmation=True).
        """
        if year is not None and month is not None:
            return year, month, False

        if month is None:
            return today.year, today.month, False

        if (today.year, month) < (today.year, today.month):
            return today.year + 1, month, True
        return today.year, month, False

    @staticmethod
    def _atomic_upsert(
        session,
        *,
        user_id,
        category_id,
        amount: Decimal,
        currency: str,
        period_start: date,
        period_end: date,
    ) -> str:
        values = {
            "usuario_id": user_id,
            "categoria_id": category_id,
            "cantidad_max": amount,
            "moneda": currency,
            "inicio_periodo": period_start,
            "fin_periodo": period_end,
        }
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect_name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            limite = LimiteCategoria(**values)
            session.add(limite)
            session.flush()
            return str(limite.id)

        statement = insert(LimiteCategoria).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                LimiteCategoria.usuario_id,
                LimiteCategoria.categoria_id,
                LimiteCategoria.inicio_periodo,
                LimiteCategoria.moneda,
            ],
            set_={
                "cantidad_max": amount,
                "fin_periodo": period_end,
                "actualizado_en": func.now(),
            },
        ).returning(LimiteCategoria.id)
        return str(session.execute(statement).scalar_one())

    # ------------------------------------------------------------------
    # Creación / edición (upsert por categoría + período)
    # ------------------------------------------------------------------

    @classmethod
    def create_limit(
        cls,
        sender_phone: str,
        data: dict,
        last_limit=None,
        today: date | None = None,
        allow_category_creation: bool = False,
        _retry_on_category_conflict: bool = True,
    ) -> LimitResult:
        """Crea o actualiza (upsert) el límite mensual de una categoría.

        `last_limit` aporta el contexto del último límite creado cuando la
        solicitud es una edición (intent change_limit).
        """
        today = today or datetime.now(ARGENTINA_TZ).date()
        sender = cls._normalize_text(sender_phone)
        if not sender:
            return cls._result("invalid_data", "sender_phone is required")
        if not isinstance(data, dict):
            return cls._result("invalid_data", "data must be a dict")

        category = cls._normalize_text(data.get("limit_category"))
        if category is None and last_limit is not None:
            category = cls._normalize_text(last_limit.category_name)

        amount = cls._normalize_amount(data.get("limit_amount"))
        if amount is None and last_limit is not None:
            amount = cls._normalize_amount(last_limit.amount)

        raw_month = data.get("limit_month")
        month = cls._normalize_month(raw_month) if raw_month is not None else None
        if raw_month is not None and month is None:
            return cls._result("invalid_month", "month must be between 1 and 12")
        if month is None and last_limit is not None:
            month = cls._normalize_month(last_limit.month)

        raw_year = data.get("limit_year")
        year = cls._normalize_year(raw_year) if raw_year is not None else None
        if raw_year is not None and year is None:
            return cls._result("invalid_year", "year must be between 1 and 9999")
        if year is None and last_limit is not None:
            year = cls._normalize_year(last_limit.year)

        currency = cls._normalize_currency(data.get("limit_currency"))
        if data.get("limit_currency") is None and last_limit is not None:
            currency = cls._normalize_currency(getattr(last_limit, "currency", "ARS"))
        if currency is None:
            return cls._result("invalid_currency", "currency must be a three-letter code")

        if amount is None:
            return cls._result(
                "needs_amount",
                "amount is required",
                category_name=category,
            )
        if category is None:
            return cls._result(
                "needs_category",
                "category is required",
                amount=amount,
                month=month,
                year=year,
                currency=currency,
            )
        if raw_year is not None and raw_month is None and last_limit is None:
            return cls._result(
                "needs_month",
                "month is required when year is provided",
                category_name=category,
                amount=amount,
                year=year,
                currency=currency,
            )

        resolved_year, resolved_month, needs_confirmation = cls._resolve_period(
            month, year, today
        )
        # Edición: el mes lo indicó el usuario pero el año se heredó del último
        # límite; si el mes ya pasó en ese año, proponer el siguiente.
        if (
            not needs_confirmation
            and raw_month is not None
            and raw_year is None
            and (resolved_year, resolved_month) < (today.year, today.month)
        ):
            resolved_year = today.year + 1
            needs_confirmation = True
        if needs_confirmation:
            return cls._result(
                "needs_year_confirmation",
                "month already passed",
                category_name=category,
                amount=amount,
                currency=currency,
                proposed_month=resolved_month,
                proposed_year=resolved_year,
            )

        inicio = date(resolved_year, resolved_month, 1)
        fin = date(
            resolved_year,
            resolved_month,
            calendar.monthrange(resolved_year, resolved_month)[1],
        )
        if fin < today:
            return cls._result(
                "expired_period",
                "cannot create a limit for an expired period",
                category_name=category,
                amount=amount,
                month=resolved_month,
                year=resolved_year,
                currency=currency,
            )

        session = SessionLocal()
        try:
            user = cls._get_user(session, sender)
            if user is None:
                return cls._result("user_not_found", "user not found")

            categoria, category_name, category_status = cls._resolve_category(
                session,
                user.id,
                category,
                allow_creation=allow_category_creation,
            )
            if category_status is not None:
                return cls._result(
                    category_status,
                    "category requires confirmation"
                    if category_status == "needs_category_confirmation"
                    else "category is outside the configured taxonomy",
                    category_name=category_name,
                    amount=amount,
                    month=resolved_month,
                    year=resolved_year,
                    currency=currency,
                )
            categoria_id = categoria.id

            # Si la solicitud es una edición del último límite (last_limit),
            # se modifica ese registro concreto (categoría, monto y período),
            # en lugar de crear/actualizar otro por (categoría + período).
            if last_limit is not None:
                edit_id = cls._uuid(getattr(last_limit, "limit_id", None))
                if edit_id is None:
                    return cls._result("stale_context", "invalid limit edit context")
                target = (
                    session.query(LimiteCategoria)
                    .filter(
                        LimiteCategoria.id == edit_id,
                        LimiteCategoria.usuario_id == user.id,
                    )
                    .with_for_update()
                    .first()
                )
                if target is None:
                    return cls._result("stale_context", "limit no longer exists")
                previous_category = cls._find_category(session, user.id, last_limit.category_name)
                if (
                    previous_category is None
                    or target.categoria_id != previous_category.id
                    or target.cantidad_max != cls._normalize_amount(last_limit.amount)
                    or target.inicio_periodo != date(last_limit.year, last_limit.month, 1)
                    or target.moneda != getattr(last_limit, "currency", "ARS")
                ):
                    return cls._result("stale_context", "limit changed since selection")
                collision = (
                    session.query(LimiteCategoria.id)
                    .filter(
                        LimiteCategoria.usuario_id == user.id,
                        LimiteCategoria.categoria_id == categoria_id,
                        LimiteCategoria.inicio_periodo == inicio,
                        LimiteCategoria.moneda == currency,
                        LimiteCategoria.id != target.id,
                    )
                    .first()
                )
                if collision is not None:
                    return cls._result(
                        "conflict",
                        "another limit already exists for that category and period",
                        category_name=category_name,
                        amount=amount,
                        month=resolved_month,
                        year=resolved_year,
                        currency=currency,
                    )
                target.cantidad_max = amount
                target.categoria_id = categoria_id
                target.moneda = currency
                target.inicio_periodo = inicio
                target.fin_periodo = fin
                session.commit()
                return cls._result(
                    "updated",
                    "limit updated",
                    limit_id=str(target.id),
                    category_name=category_name,
                    amount=amount,
                    month=resolved_month,
                    year=resolved_year,
                    currency=currency,
                )

            existing = (
                session.query(LimiteCategoria)
                .filter(
                    LimiteCategoria.usuario_id == user.id,
                    LimiteCategoria.categoria_id == categoria_id,
                    LimiteCategoria.inicio_periodo == inicio,
                    LimiteCategoria.moneda == currency,
                )
                .first()
            )

            if existing is not None:
                existing.cantidad_max = amount
                existing.fin_periodo = fin
                session.commit()
                return cls._result(
                    "updated",
                    "limit updated",
                    limit_id=str(existing.id),
                    category_name=category_name,
                    amount=amount,
                    month=resolved_month,
                    year=resolved_year,
                    currency=currency,
                )

            limit_id = cls._atomic_upsert(
                session,
                user_id=user.id,
                category_id=categoria_id,
                amount=amount,
                currency=currency,
                period_start=inicio,
                period_end=fin,
            )
            session.commit()
            return cls._result(
                "created",
                "limit created",
                limit_id=limit_id,
                category_name=category_name,
                amount=amount,
                month=resolved_month,
                year=resolved_year,
                currency=currency,
            )

        except IntegrityError as exc:
            session.rollback()
            if allow_category_creation and _retry_on_category_conflict:
                print(
                    "[LIMIT_CATEGORY] Concurrent category creation detected; retrying "
                    f"category={category}"
                )
                session.close()
                return cls.create_limit(
                    sender_phone,
                    data,
                    last_limit=last_limit,
                    today=today,
                    allow_category_creation=allow_category_creation,
                    _retry_on_category_conflict=False,
                )
            print(
                "[LIMIT_CREATION] Persistence integrity error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not persist limit")

        except SQLAlchemyError as exc:
            session.rollback()
            print(
                "[LIMIT_CREATION] Persistence error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not persist limit")

        except Exception as exc:
            session.rollback()
            print(
                "[LIMIT_CREATION] Error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not persist limit")

        finally:
            session.close()

    # ------------------------------------------------------------------
    # Listado (solo límites vigentes; los vencidos se filtran)
    # ------------------------------------------------------------------

    @classmethod
    def list_limits(
        cls,
        user_id,
        today: date | None = None,
    ) -> LimitListResult:
        today = today or datetime.now(ARGENTINA_TZ).date()
        session = SessionLocal()
        try:
            rows = (
                session.query(LimiteCategoria, Categoria)
                .join(Categoria, LimiteCategoria.categoria_id == Categoria.id)
                .filter(
                    LimiteCategoria.usuario_id == user_id,
                    LimiteCategoria.fin_periodo >= today,
                    Categoria.esta_eliminado.is_(False),
                )
                .order_by(
                    LimiteCategoria.inicio_periodo.asc(),
                    Categoria.nombre.asc(),
                    LimiteCategoria.moneda.asc(),
                )
                .all()
            )
            entries = [
                LimitEntry(
                    category_name=categoria.nombre,
                    amount=limite.cantidad_max,
                    month=limite.inicio_periodo.month,
                    year=limite.inicio_periodo.year,
                    currency=limite.moneda,
                    id=str(limite.id),
                )
                for limite, categoria in rows
            ]
            return LimitListResult(
                status="ok",
                message="limits listed",
                limits=entries,
            )
        except Exception as exc:
            print(f"[LIMIT_LIST] Error: {type(exc).__name__}: {exc}")
            return LimitListResult(status="error", message="could not list limits")
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Eliminación
    # ------------------------------------------------------------------

    @classmethod
    def find_limit_candidates(
        cls, sender_phone: str, *, category: str | None = None,
        month: int | None = None, year: int | None = None,
        currency: str | None = None, today: date | None = None,
    ) -> list[dict[str, Any]]:
        today = today or datetime.now(ARGENTINA_TZ).date()
        session = SessionLocal()
        try:
            user = cls._get_user(session, sender_phone)
            if user is None:
                return []
            query = (
                session.query(LimiteCategoria, Categoria.nombre)
                .join(Categoria, LimiteCategoria.categoria_id == Categoria.id)
                .filter(LimiteCategoria.usuario_id == user.id)
                .filter(LimiteCategoria.fin_periodo >= today)
                .filter(Categoria.esta_eliminado.is_(False))
            )
            if category:
                query = query.filter(func.lower(Categoria.nombre) == category.strip().lower())
            if currency:
                query = query.filter(LimiteCategoria.moneda == currency.upper())
            if month:
                query = query.filter(func.extract("month", LimiteCategoria.inicio_periodo) == month)
            if year:
                query = query.filter(func.extract("year", LimiteCategoria.inicio_periodo) == year)
            rows = query.order_by(LimiteCategoria.inicio_periodo, LimiteCategoria.id).limit(20).all()
            return [
                {"limit_id": str(item.id), "category": name, "amount": str(item.cantidad_max),
                 "month": item.inicio_periodo.month, "year": item.inicio_periodo.year,
                 "currency": item.moneda, "label": name}
                for item, name in rows
            ]
        finally:
            session.close()

    @classmethod
    def delete_limits_by_ids(
        cls, sender_phone: str, candidates: list[dict[str, Any]],
    ) -> LimitBatchResult:
        """Delete exactly the displayed limits, atomically and only for their owner."""
        if not candidates or len(candidates) != len({item.get("limit_id") for item in candidates}):
            return LimitBatchResult("invalid_data")
        try:
            ids = [UUID(str(item["limit_id"])) for item in candidates]
        except (ValueError, TypeError, KeyError):
            return LimitBatchResult("invalid_data")
        session = SessionLocal()
        try:
            user = cls._get_user(session, sender_phone)
            if user is None:
                return LimitBatchResult("user_not_found")
            rows = (
                session.query(LimiteCategoria)
                .filter(LimiteCategoria.usuario_id == user.id)
                .filter(LimiteCategoria.id.in_(ids))
                .with_for_update()
                .all()
            )
            by_id = {str(row.id): row for row in rows}
            category_names = {
                str(row.id): session.get(Categoria, row.categoria_id).nombre
                for row in rows
            }
            today = datetime.now(ARGENTINA_TZ).date()
            for candidate in candidates:
                row = by_id.get(candidate["limit_id"])
                if (
                    row is None or row.fin_periodo < today
                    or row.inicio_periodo.month != candidate.get("month")
                    or row.inicio_periodo.year != candidate.get("year")
                    or row.moneda != candidate.get("currency", "ARS")
                    or str(row.cantidad_max) != str(Decimal(str(candidate.get("amount"))))
                    or category_names[str(row.id)].casefold() != str(candidate.get("category", "")).casefold()
                ):
                    return LimitBatchResult("stale_context")
            for row in rows:
                session.delete(row)
            session.commit()
            return LimitBatchResult("deleted", candidates)
        except Exception as exc:
            session.rollback()
            print(f"[LIMIT_BATCH_DELETE] Error: {type(exc).__name__}: {exc}")
            return LimitBatchResult("persistence_error")
        finally:
            session.close()

    @classmethod
    def delete_limit(
        cls,
        sender_phone: str,
        category: str,
        month: int | None = None,
        year: int | None = None,
        currency: str | None = None,
        today: date | None = None,
    ) -> LimitResult:
        """Elimina un límite vigente por categoría.

        Sin mes: si hay un único límite vigente lo borra; si hay varios,
        devuelve needs_month_selection con los candidatos (mes + valor).
        """
        today = today or datetime.now(ARGENTINA_TZ).date()
        sender = cls._normalize_text(sender_phone)
        category = cls._normalize_text(category)
        if not sender:
            return cls._result("invalid_data", "sender_phone is required")
        if not category:
            return cls._result("invalid_data", "category is required")

        raw_month = month
        raw_year = year
        month = cls._normalize_month(month)
        year = cls._normalize_year(year)
        raw_currency = currency
        currency = cls._normalize_currency(currency) if currency is not None else None
        if raw_month is not None and month is None:
            return cls._result("invalid_month", "month must be between 1 and 12")
        if raw_year is not None and year is None:
            return cls._result("invalid_year", "year must be between 1 and 9999")
        if raw_currency is not None and currency is None:
            return cls._result("invalid_currency", "invalid currency")

        session = SessionLocal()
        try:
            user = cls._get_user(session, sender)
            if user is None:
                return cls._result("user_not_found", "user not found")

            categoria = cls._find_category(session, user.id, category)
            if categoria is None:
                return cls._result("not_found", "category has no limits")

            limits = (
                session.query(LimiteCategoria)
                .filter(
                    LimiteCategoria.usuario_id == user.id,
                    LimiteCategoria.categoria_id == categoria.id,
                    LimiteCategoria.fin_periodo >= today,
                )
                .order_by(
                    LimiteCategoria.inicio_periodo.asc(),
                    LimiteCategoria.moneda.asc(),
                )
                .all()
            )
            if currency is not None:
                limits = [lim for lim in limits if lim.moneda == currency]
            if not limits:
                return cls._result("not_found", "category has no limits")

            if month is None:
                if len(limits) == 1:
                    target = limits[0]
                else:
                    candidates = [
                        {
                            "limit_id": str(lim.id),
                            "month": lim.inicio_periodo.month,
                            "year": lim.inicio_periodo.year,
                            "amount": str(lim.cantidad_max),
                            "currency": lim.moneda,
                        }
                        for lim in limits
                    ]
                    return cls._result(
                        "needs_month_selection",
                        "select the month",
                        category_name=categoria.nombre,
                        candidates=candidates,
                    )
            else:
                month_matches = [
                    lim for lim in limits if lim.inicio_periodo.month == month
                ]
                if not month_matches:
                    return cls._result(
                        "not_found",
                        "no limit for that month",
                        category_name=categoria.nombre,
                    )
                if year is not None:
                    year_matches = [
                        lim for lim in month_matches if lim.inicio_periodo.year == year
                    ]
                    if not year_matches:
                        return cls._result(
                            "not_found",
                            "no limit for that month and year",
                            category_name=categoria.nombre,
                        )
                    month_matches = year_matches
                if len(month_matches) > 1 and currency is None:
                    return cls._result(
                        "needs_month_selection",
                        "select the currency",
                        category_name=categoria.nombre,
                        candidates=[
                            {
                                "limit_id": str(lim.id),
                                "month": lim.inicio_periodo.month,
                                "year": lim.inicio_periodo.year,
                                "amount": str(lim.cantidad_max),
                                "currency": lim.moneda,
                            }
                            for lim in month_matches
                        ],
                    )
                target = month_matches[0]

            session.delete(target)
            session.commit()
            return cls._result(
                "deleted",
                "limit deleted",
                limit_id=str(target.id),
                category_name=categoria.nombre,
                amount=target.cantidad_max,
                month=target.inicio_periodo.month,
                year=target.inicio_periodo.year,
                currency=target.moneda,
            )

        except Exception as exc:
            session.rollback()
            print(
                "[LIMIT_DELETE] Error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not delete limit")

        finally:
            session.close()
