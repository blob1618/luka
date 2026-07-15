import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import matplotlib.pyplot as plt
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models.database import Categoria, MovimientoFinanciero, SessionLocal, Usuario


@dataclass
class MovementRegistrationResult:
    status: str
    message: str
    movement_id: str | None = None
    user_id: str | None = None
    duplicate: bool = False


@dataclass
class CategoryResult:
    status: str                # "created" | "already_exists" | "deleted" | "not_found" | "error"
    message: str
    category_id: str | None = None
    category_name: str | None = None


@dataclass
class CategoryWithTotals:
    category_id: str
    category_name: str
    es_default: bool
    total_ingresos: Decimal = Decimal("0")
    total_egresos: Decimal = Decimal("0")


@dataclass
class CategoriesListResult:
    status: str
    message: str
    categories: list[CategoryWithTotals] = field(default_factory=list)


class FinanceService:
    VALID_MOVEMENT_TYPES = {"ingreso", "egreso"}

    @staticmethod
    def _result(
        status: str,
        message: str,
        movement_id: str | None = None,
        user_id: str | None = None,
        duplicate: bool = False,
    ) -> MovementRegistrationResult:
        return MovementRegistrationResult(
            status=status,
            message=message,
            movement_id=movement_id,
            user_id=user_id,
            duplicate=duplicate,
        )

    @staticmethod
    def _normalize_optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_amount(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not amount.is_finite() or amount <= 0:
            return None
        return amount

    @classmethod
    def _resolve_description(cls, llm_result: dict[str, Any], original_text: str) -> str | None:
        for field_name in ("description", "expense"):
            text = cls._normalize_optional_text(llm_result.get(field_name))
            if text:
                return text[:500]

        text = cls._normalize_optional_text(original_text)
        if text:
            return text[:500]

        return None

    @classmethod
    def _find_duplicate(
        cls, session, whatsapp_message_id: str | None
    ) -> MovimientoFinanciero | None:
        if not whatsapp_message_id:
            return None
        return (
            session.query(MovimientoFinanciero)
            .filter(MovimientoFinanciero.whatsapp_message_id == whatsapp_message_id)
            .first()
        )

    @staticmethod
    def _find_category(session, user_id: Any, category_name: str | None) -> Categoria | None:
        if not category_name:
            return None

        normalized_name = category_name.strip().lower()
        if not normalized_name:
            return None

        return (
            session.query(Categoria)
            .filter(Categoria.usuario_id == user_id)
            .filter(Categoria.esta_eliminado.is_(False))
            .filter(func.lower(func.trim(Categoria.nombre)) == normalized_name)
            .first()
        )

    @classmethod
    def register_movement_from_whatsapp_text(
        cls,
        sender_phone: str,
        whatsapp_message_id: str | None,
        original_text: str,
        llm_result: dict,
    ) -> MovementRegistrationResult:
        sender_phone = cls._normalize_optional_text(sender_phone)
        if not sender_phone:
            return cls._result("invalid_data", "sender_phone is required")

        if not isinstance(llm_result, dict):
            return cls._result("invalid_data", "llm_result must be a dict")

        intent = cls._normalize_optional_text(llm_result.get("intent"))
        intent = intent.lower() if intent else None
        raw_movement_type = cls._normalize_optional_text(llm_result.get("movement_type"))
        if raw_movement_type is None:
            if intent == "expense":
                return cls._result("invalid_data", "movement_type is required")
            return cls._result("not_a_movement", "message is not a financial movement")

        movement_type = raw_movement_type.lower()
        if movement_type not in cls.VALID_MOVEMENT_TYPES:
            if intent == "expense":
                return cls._result("invalid_data", "movement_type must be ingreso or egreso")
            return cls._result("not_a_movement", "message is not a financial movement")

        amount = cls._normalize_amount(llm_result.get("amount"))
        if amount is None:
            return cls._result("invalid_data", "amount must be a positive number")

        raw_currency = llm_result.get("currency")
        currency = "ARS" if raw_currency is None else str(raw_currency).strip().upper()
        if not currency:
            return cls._result("invalid_data", "currency is required")

        description = cls._resolve_description(llm_result, original_text)
        if not description:
            return cls._result("invalid_data", "description is required")

        whatsapp_message_id = cls._normalize_optional_text(whatsapp_message_id)
        category_name = cls._normalize_optional_text(llm_result.get("category"))

        session = SessionLocal()
        try:
            user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
            if user is None:
                return cls._result("user_not_found", "user not found")

            user_id = str(user.id)

            duplicate = cls._find_duplicate(session, whatsapp_message_id)
            if duplicate is not None:
                return cls._result(
                    "duplicate",
                    "movement already registered",
                    movement_id=str(duplicate.id),
                    user_id=str(duplicate.usuario_id),
                    duplicate=True,
                )

            category = cls._find_category(session, user.id, category_name)
            movement = MovimientoFinanciero(
                usuario_id=user.id,
                categoria_id=category.id if category else None,
                tipo=movement_type,
                cantidad=amount,
                moneda=currency,
                descripcion=description,
                origen="whatsapp_text",
                whatsapp_message_id=whatsapp_message_id,
            )

            session.add(movement)
            session.commit()

            return cls._result(
                "registered",
                "movement registered",
                movement_id=str(movement.id),
                user_id=user_id,
                duplicate=False,
            )

        except IntegrityError as exc:
            session.rollback()
            print(
                "[MOVEMENT_REGISTRATION] Persistence integrity error: "
                f"{type(exc.orig).__name__}: {exc.orig}"
            )
            if whatsapp_message_id:
                try:
                    duplicate = cls._find_duplicate(session, whatsapp_message_id)
                except Exception:
                    duplicate = None
                if duplicate is not None:
                    return cls._result(
                        "duplicate",
                        "movement already registered",
                        movement_id=str(duplicate.id),
                        user_id=str(duplicate.usuario_id),
                        duplicate=True,
                    )
            return cls._result("persistence_error", "could not persist movement")

        except Exception as exc:
            session.rollback()
            print(
                "[MOVEMENT_REGISTRATION] Persistence error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not persist movement")

        finally:
            session.close()

    # ------------------------------------------------------------------
    # STK-39: Gestión de categorías
    # ------------------------------------------------------------------

    @classmethod
    def _normalize_category_name(cls, name: str) -> str:
        """Normaliza un nombre de categoría: lowercase + trim."""
        return name.strip().lower()

    @classmethod
    def _get_user_by_phone(cls, session, sender_phone: str):
        return session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()

    @classmethod
    def create_category(
        cls,
        user_id: Any,
        category_name: str,
        es_default: bool = False,
    ) -> CategoryResult:
        """
        Crea una categoría si no existe una activa con el mismo nombre (case-insensitive).
        Si ya existe una activa, retorna 'already_exists'.
        Si existe una eliminada con el mismo nombre, la reactiva.
        """
        normalized = cls._normalize_category_name(category_name)
        if not normalized:
            return CategoryResult(status="error", message="category name is required")

        session = SessionLocal()
        try:
            # Buscar si ya existe una activa con ese nombre
            existing_active = (
                session.query(Categoria)
                .filter(Categoria.usuario_id == user_id)
                .filter(Categoria.esta_eliminado.is_(False))
                .filter(func.lower(func.trim(Categoria.nombre)) == normalized)
                .first()
            )
            if existing_active is not None:
                return CategoryResult(
                    status="already_exists",
                    message=f"La categoría '{existing_active.nombre}' ya existe.",
                    category_id=str(existing_active.id),
                    category_name=existing_active.nombre,
                )

            # Buscar si existe una eliminada para reactivar
            existing_deleted = (
                session.query(Categoria)
                .filter(Categoria.usuario_id == user_id)
                .filter(Categoria.esta_eliminado.is_(True))
                .filter(func.lower(func.trim(Categoria.nombre)) == normalized)
                .first()
            )
            if existing_deleted is not None:
                existing_deleted.esta_eliminado = False
                session.commit()
                return CategoryResult(
                    status="created",
                    message=f"Categoría '{existing_deleted.nombre}' reactivada.",
                    category_id=str(existing_deleted.id),
                    category_name=existing_deleted.nombre,
                )

            # Crear nueva categoría
            categoria = Categoria(
                usuario_id=user_id,
                nombre=category_name.strip(),
                es_default=es_default,
                esta_eliminado=False,
            )
            session.add(categoria)
            session.commit()
            return CategoryResult(
                status="created",
                message=f"Categoría '{categoria.nombre}' creada.",
                category_id=str(categoria.id),
                category_name=categoria.nombre,
            )

        except Exception as exc:
            session.rollback()
            print(f"[FINANCE] create_category error: {type(exc).__name__}: {exc}")
            return CategoryResult(status="error", message="could not create category")

        finally:
            session.close()

    @classmethod
    def delete_category(cls, user_id: Any, category_name: str) -> CategoryResult:
        """
        Elimina (soft-delete) una categoría por nombre (case-insensitive).
        Pone categoria_id = NULL en todos los movimientos asociados.
        """
        normalized = cls._normalize_category_name(category_name)
        if not normalized:
            return CategoryResult(status="error", message="category name is required")

        session = SessionLocal()
        try:
            categoria = (
                session.query(Categoria)
                .filter(Categoria.usuario_id == user_id)
                .filter(Categoria.esta_eliminado.is_(False))
                .filter(func.lower(func.trim(Categoria.nombre)) == normalized)
                .first()
            )
            if categoria is None:
                return CategoryResult(
                    status="not_found",
                    message=f"No encontré una categoría '{category_name.strip()}'.",
                )

            # Soft delete
            categoria.esta_eliminado = True

            # Desvincular movimientos
            session.query(MovimientoFinanciero).filter(
                MovimientoFinanciero.categoria_id == categoria.id
            ).update({"categoria_id": None})

            session.commit()
            return CategoryResult(
                status="deleted",
                message=f"Categoría '{categoria.nombre}' eliminada.",
                category_id=str(categoria.id),
                category_name=categoria.nombre,
            )

        except Exception as exc:
            session.rollback()
            print(f"[FINANCE] delete_category error: {type(exc).__name__}: {exc}")
            return CategoryResult(status="error", message="could not delete category")

        finally:
            session.close()

    @classmethod
    def get_categories_with_totals(cls, user_id: Any) -> CategoriesListResult:
        """
        Retorna todas las categorías activas del usuario con totales
        de ingresos y egresos registrados.
        """
        session = SessionLocal()
        try:
            categorias = (
                session.query(Categoria)
                .filter(Categoria.usuario_id == user_id)
                .filter(Categoria.esta_eliminado.is_(False))
                .order_by(Categoria.nombre)
                .all()
            )

            result_categories: list[CategoryWithTotals] = []
            for cat in categorias:
                # Totales de ingresos
                ingreso_total = (
                    session.query(func.coalesce(func.sum(MovimientoFinanciero.cantidad), 0))
                    .filter(MovimientoFinanciero.categoria_id == cat.id)
                    .filter(MovimientoFinanciero.tipo == "ingreso")
                    .scalar()
                ) or Decimal("0")

                # Totales de egresos
                egreso_total = (
                    session.query(func.coalesce(func.sum(MovimientoFinanciero.cantidad), 0))
                    .filter(MovimientoFinanciero.categoria_id == cat.id)
                    .filter(MovimientoFinanciero.tipo == "egreso")
                    .scalar()
                ) or Decimal("0")

                result_categories.append(CategoryWithTotals(
                    category_id=str(cat.id),
                    category_name=cat.nombre,
                    es_default=cat.es_default or False,
                    total_ingresos=ingreso_total,
                    total_egresos=egreso_total,
                ))

            return CategoriesListResult(
                status="ok",
                message=f"Se encontraron {len(result_categories)} categorías.",
                categories=result_categories,
            )

        except Exception as exc:
            print(f"[FINANCE] get_categories_with_totals error: {type(exc).__name__}: {exc}")
            return CategoriesListResult(
                status="error",
                message="could not retrieve categories",
            )

        finally:
            session.close()

    @classmethod
    def register_movement_with_category(
        cls,
        sender_phone: str,
        whatsapp_message_id: str | None,
        original_text: str,
        movement_type: str,
        amount: Decimal,
        currency: str,
        description: str,
        category_name: str | None = None,
        create_category_if_missing: bool = False,
    ) -> MovementRegistrationResult:
        """
        Registra un movimiento financiero con una categoría específica.
        A diferencia de register_movement_from_whatsapp_text, esta función:
        - Recibe los datos ya parseados (no un llm_result)
        - Si create_category_if_missing=True y la categoría no existe, la crea automáticamente
        """
        sender_phone = cls._normalize_optional_text(sender_phone)
        if not sender_phone:
            return cls._result("invalid_data", "sender_phone is required")

        if movement_type not in cls.VALID_MOVEMENT_TYPES:
            return cls._result("invalid_data", "movement_type must be ingreso or egreso")

        if amount is None or amount <= 0:
            return cls._result("invalid_data", "amount must be a positive number")

        if not description:
            return cls._result("invalid_data", "description is required")

        whatsapp_message_id = cls._normalize_optional_text(whatsapp_message_id)

        session = SessionLocal()
        try:
            user = cls._get_user_by_phone(session, sender_phone)
            if user is None:
                return cls._result("user_not_found", "user not found")

            user_id = str(user.id)

            duplicate = cls._find_duplicate(session, whatsapp_message_id)
            if duplicate is not None:
                return cls._result(
                    "duplicate",
                    "movement already registered",
                    movement_id=str(duplicate.id),
                    user_id=str(duplicate.usuario_id),
                    duplicate=True,
                )

            # Resolver categoría
            categoria_id = None
            if category_name:
                category = cls._find_category(session, user.id, category_name)
                if category is not None:
                    categoria_id = category.id
                elif create_category_if_missing:
                    cat_result = cls.create_category(user.id, category_name)
                    if cat_result.status == "created":
                        # Reabrir sesión (create_category cierra su propia sesión)
                        session.close()
                        session = SessionLocal()
                        # Volver a buscar el usuario
                        user = cls._get_user_by_phone(session, sender_phone)
                        if user is None:
                            return cls._result("user_not_found", "user not found after category creation")
                        # Convertir string a UUID para la consulta
                        from uuid import UUID as UuidType
                        cat_uuid = UuidType(cat_result.category_id)
                        categoria = (
                            session.query(Categoria)
                            .filter(Categoria.id == cat_uuid)
                            .first()
                        )
                        if categoria:
                            categoria_id = categoria.id

            movement = MovimientoFinanciero(
                usuario_id=user.id,
                categoria_id=categoria_id,
                tipo=movement_type,
                cantidad=amount,
                moneda=currency,
                descripcion=description,
                origen="whatsapp_text",
                whatsapp_message_id=whatsapp_message_id,
            )

            session.add(movement)
            session.commit()

            return cls._result(
                "registered",
                "movement registered",
                movement_id=str(movement.id),
                user_id=user_id,
                duplicate=False,
            )

        except IntegrityError as exc:
            session.rollback()
            print(f"[MOVEMENT_REGISTRATION] Integrity error: {type(exc.orig).__name__}: {exc.orig}")
            if whatsapp_message_id:
                try:
                    duplicate = cls._find_duplicate(session, whatsapp_message_id)
                except Exception:
                    duplicate = None
                if duplicate is not None:
                    return cls._result(
                        "duplicate",
                        "movement already registered",
                        movement_id=str(duplicate.id),
                        user_id=str(duplicate.usuario_id),
                        duplicate=True,
                    )
            return cls._result("persistence_error", "could not persist movement")

        except Exception as exc:
            session.rollback()
            print(f"[MOVEMENT_REGISTRATION] Persistence error: {type(exc).__name__}: {exc}")
            return cls._result("persistence_error", "could not persist movement")

        finally:
            session.close()

    @staticmethod
    def check_dynamic_budget(user_id: int, new_expense: float, category: str) -> str:
        """
        Calcula si un gasto supera el presupuesto.
        Si es así, genera un mensaje positivo de reasignación (El Fin de la 'Espiral de Culpa').
        """
        # TODO: Consultar DB para comparar presupuestos vs gastos
        return "¡Buen registro! Te pasaste un poco en ocio, pero ajustamos el límite de ropa de este mes para que sigas en carrera. ¡Vamos bien!"

    @staticmethod
    def generate_expense_chart(expenses_by_category: dict) -> bytes:
        """
        Genera un grafico de torta basico y lo retorna como bytes.
        """
        labels = list(expenses_by_category.keys())
        sizes = list(expenses_by_category.values())

        fig, ax = plt.subplots(figsize=(6, 6))
        ax.pie(sizes, labels=labels, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")

        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        buf.seek(0)

        plt.close(fig)
        return buf.read()
