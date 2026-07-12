import io
from dataclasses import dataclass
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

        except IntegrityError:
            session.rollback()
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

        except Exception:
            session.rollback()
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
