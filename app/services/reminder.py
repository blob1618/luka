from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.database import Recordatorio, SessionLocal, Usuario


@dataclass
class ReminderResult:
    status: str
    message: str
    reminder_id: str | None = None


class ReminderService:

    @staticmethod
    def _normalize_text(value: Any, max_length: int = 200) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:max_length]

    @staticmethod
    def _normalize_day(value: Any) -> int | None:
        if value is None:
            return None
        try:
            day = int(value)
        except (TypeError, ValueError):
            return None
        if day < 1 or day > 31:
            return None
        return day

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

    @staticmethod
    def _result(
        status: str,
        message: str,
        reminder_id: str | None = None,
    ) -> ReminderResult:
        return ReminderResult(status=status, message=message, reminder_id=reminder_id)

    @classmethod
    def create_reminder(
        cls,
        sender_phone: str,
        llm_result: dict,
    ) -> ReminderResult:
        sender_phone = cls._normalize_text(sender_phone)
        if not sender_phone:
            return cls._result("invalid_data", "sender_phone is required")

        if not isinstance(llm_result, dict):
            return cls._result("invalid_data", "llm_result must be a dict")

        # Validar concepto
        concept = cls._normalize_text(llm_result.get("reminder_concept"))
        if not concept:
            return cls._result("invalid_data", "¿Qué pago querés que te recuerde?")

        # Validar día
        raw_day = llm_result.get("reminder_day")
        if raw_day is None:
            return cls._result(
                "invalid_data",
                f"¿Qué día del mes vence {concept}?",
            )

        day = cls._normalize_day(raw_day)
        if day is None:
            return cls._result(
                "invalid_data",
                "El día del mes debe ser un número entre 1 y 31.",
            )

        # Validar monto (opcional)
        raw_amount = llm_result.get("reminder_amount")
        amount = None
        if raw_amount is not None:
            amount = cls._normalize_amount(raw_amount)
            if amount is None:
                return cls._result(
                    "invalid_data",
                    "El monto debe ser un número positivo.",
                )

        # Normalizar moneda
        raw_currency = llm_result.get("reminder_currency")
        currency = "ARS"
        if raw_currency is not None:
            currency = str(raw_currency).strip().upper() or "ARS"

        # Buscar usuario
        session = SessionLocal()
        try:
            user = (
                session.query(Usuario)
                .filter(Usuario.whatsapp_id == sender_phone)
                .first()
            )
            if user is None:
                return cls._result("user_not_found", "user not found")

            recordatorio = Recordatorio(
                usuario_id=user.id,
                titulo=concept,
                dia_del_mes=day,
                monto=amount,
                moneda=currency,
                estado="activo",
            )
            session.add(recordatorio)
            session.commit()

            return cls._result(
                "created",
                "reminder created",
                reminder_id=str(recordatorio.id),
            )

        except Exception as exc:
            session.rollback()
            print(
                "[REMINDER_CREATION] Persistence error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not persist reminder")

        finally:
            session.close()
