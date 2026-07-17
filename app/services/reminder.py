from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.models.database import Recordatorio, SessionLocal, Usuario


@dataclass
class ReminderResult:
    status: str
    message: str
    reminder_id: str | None = None


@dataclass
class ReminderListResult:
    status: str
    message: str
    reminders: list[dict[str, Any]] | None = None


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
    def _normalize_reminder_id(value: Any) -> UUID | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return UUID(text)
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _result(
        status: str,
        message: str,
        reminder_id: str | None = None,
    ) -> ReminderResult:
        return ReminderResult(status=status, message=message, reminder_id=reminder_id)

    @classmethod
    def _validate_reminder_data(
        cls,
        llm_result: dict,
        existing_reminder: Recordatorio | None = None,
    ) -> tuple[dict[str, Any] | None, ReminderResult | None]:
        if not isinstance(llm_result, dict):
            return None, cls._result("invalid_data", "llm_result must be a dict")

        concept = cls._normalize_text(llm_result.get("reminder_concept"))
        if concept is None:
            if existing_reminder is None:
                return None, cls._result("invalid_data", "¿Qué pago querés que te recuerde?")
            concept = cls._normalize_text(existing_reminder.titulo)

        raw_day = llm_result.get("reminder_day")
        if raw_day is None:
            if existing_reminder is None:
                return None, cls._result(
                    "invalid_data",
                    f"¿Qué día del mes vence {concept}?",
                )
            day = existing_reminder.dia_del_mes
        else:
            day = cls._normalize_day(raw_day)
            if day is None:
                return None, cls._result(
                    "invalid_data",
                    "El día del mes debe ser un número entre 1 y 31.",
                )

        raw_amount = llm_result.get("reminder_amount")
        if raw_amount is None:
            amount = existing_reminder.monto if existing_reminder is not None else None
        else:
            amount = cls._normalize_amount(raw_amount)
            if amount is None:
                return None, cls._result(
                    "invalid_data",
                    "El monto debe ser un número positivo.",
                )

        raw_currency = llm_result.get("reminder_currency")
        if raw_currency is None:
            currency = existing_reminder.moneda if existing_reminder is not None else "ARS"
        else:
            currency = str(raw_currency).strip().upper() or (
                existing_reminder.moneda if existing_reminder is not None else "ARS"
            )

        return (
            {
                "concept": concept,
                "day": day,
                "amount": amount,
                "currency": currency,
            },
            None,
        )

    @staticmethod
    def _reminder_to_dict(reminder: Recordatorio) -> dict[str, Any]:
        return {
            "id": str(reminder.id),
            "titulo": reminder.titulo,
            "dia_del_mes": reminder.dia_del_mes,
            "monto": reminder.monto,
            "moneda": reminder.moneda,
            "estado": reminder.estado,
        }

    @classmethod
    def _get_user(cls, session, sender_phone: str):
        return (
            session.query(Usuario)
            .filter(Usuario.whatsapp_id == sender_phone)
            .first()
        )

    @classmethod
    def _get_owned_reminder(
        cls,
        session,
        user_id,
        reminder_id: str,
    ) -> tuple[Recordatorio | None, ReminderResult | None]:
        normalized_id = cls._normalize_reminder_id(reminder_id)
        if not normalized_id:
            return None, cls._result("invalid_data", "reminder_id is required")

        reminder = (
            session.query(Recordatorio)
            .filter(Recordatorio.id == normalized_id)
            .first()
        )
        if reminder is None:
            return None, cls._result("not_found", "reminder not found")

        if reminder.usuario_id != user_id:
            return None, cls._result("not_owned", "reminder does not belong to user")

        return reminder, None

    @classmethod
    def create_reminder(
        cls,
        sender_phone: str,
        llm_result: dict,
    ) -> ReminderResult:
        sender_phone = cls._normalize_text(sender_phone)
        if not sender_phone:
            return cls._result("invalid_data", "sender_phone is required")

        validated_data, error = cls._validate_reminder_data(llm_result)
        if error is not None or validated_data is None:
            return error or cls._result("invalid_data", "invalid reminder data")

        session = SessionLocal()
        try:
            user = cls._get_user(session, sender_phone)
            if user is None:
                return cls._result("user_not_found", "user not found")

            if cls.title_exists(session, user.id, validated_data["concept"]):
                return cls._result(
                    "duplicate_title",
                    f"Ya tenés un recordatorio llamado \"{validated_data['concept']}\". ¿Querés usar otro nombre?",
                )

            recordatorio = Recordatorio(
                usuario_id=user.id,
                titulo=validated_data["concept"],
                dia_del_mes=validated_data["day"],
                monto=validated_data["amount"],
                moneda=validated_data["currency"],
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

    @classmethod
    def list_reminders(cls, user_id) -> ReminderListResult:
        session = SessionLocal()
        try:
            reminders = (
                session.query(Recordatorio)
                .filter(
                    Recordatorio.usuario_id == user_id,
                    Recordatorio.estado == "activo",
                )
                .order_by(Recordatorio.dia_del_mes.asc())
                .all()
            )
            return ReminderListResult(
                status="ok",
                message="reminders listed",
                reminders=[cls._reminder_to_dict(reminder) for reminder in reminders],
            )
        finally:
            session.close()

    @classmethod
    def update_reminder(
        cls,
        sender_phone: str,
        reminder_id: str,
        llm_result: dict,
    ) -> ReminderResult:
        sender_phone = cls._normalize_text(sender_phone)
        if not sender_phone:
            return cls._result("invalid_data", "sender_phone is required")

        session = SessionLocal()
        try:
            user = cls._get_user(session, sender_phone)
            if user is None:
                return cls._result("user_not_found", "user not found")

            reminder, error = cls._get_owned_reminder(session, user.id, reminder_id)
            if error is not None or reminder is None:
                return error or cls._result("not_found", "reminder not found")

            validated_data, validation_error = cls._validate_reminder_data(
                llm_result,
                existing_reminder=reminder,
            )
            if validation_error is not None or validated_data is None:
                return validation_error or cls._result("invalid_data", "invalid reminder data")

            reminder.titulo = validated_data["concept"]
            reminder.dia_del_mes = validated_data["day"]
            reminder.monto = validated_data["amount"]
            reminder.moneda = validated_data["currency"]
            session.commit()

            return cls._result("updated", "reminder updated", reminder_id=str(reminder.id))

        except Exception as exc:
            session.rollback()
            print(
                "[REMINDER_UPDATE] Persistence error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not update reminder")

        finally:
            session.close()

    @classmethod
    def _change_state(
        cls,
        sender_phone: str,
        reminder_id: str,
        target_state: str,
        allowed_states: set[str],
        success_status: str,
        success_message: str,
    ) -> ReminderResult:
        sender_phone = cls._normalize_text(sender_phone)
        if not sender_phone:
            return cls._result("invalid_data", "sender_phone is required")

        session = SessionLocal()
        try:
            user = cls._get_user(session, sender_phone)
            if user is None:
                return cls._result("user_not_found", "user not found")

            reminder, error = cls._get_owned_reminder(session, user.id, reminder_id)
            if error is not None or reminder is None:
                return error or cls._result("not_found", "reminder not found")

            if reminder.estado not in allowed_states:
                return cls._result("invalid_data", "reminder state does not allow this operation")

            reminder.estado = target_state
            session.commit()
            return cls._result(success_status, success_message, reminder_id=str(reminder.id))

        except Exception as exc:
            session.rollback()
            print(
                f"[REMINDER_{success_status.upper()}] Persistence error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", f"could not {success_message}")

        finally:
            session.close()

    @classmethod
    def pause_reminder(cls, sender_phone: str, reminder_id: str) -> ReminderResult:
        return cls._change_state(
            sender_phone=sender_phone,
            reminder_id=reminder_id,
            target_state="pausado",
            allowed_states={"activo"},
            success_status="paused",
            success_message="reminder paused",
        )

    @classmethod
    def activate_reminder(cls, sender_phone: str, reminder_id: str) -> ReminderResult:
        return cls._change_state(
            sender_phone=sender_phone,
            reminder_id=reminder_id,
            target_state="activo",
            allowed_states={"pausado"},
            success_status="activated",
            success_message="reminder activated",
        )

    @classmethod
    def delete_reminder(cls, sender_phone: str, reminder_id: str) -> ReminderResult:
        sender_phone = cls._normalize_text(sender_phone)
        if not sender_phone:
            return cls._result("invalid_data", "sender_phone is required")

        session = SessionLocal()
        try:
            user = cls._get_user(session, sender_phone)
            if user is None:
                return cls._result("user_not_found", "user not found")

            reminder, error = cls._get_owned_reminder(session, user.id, reminder_id)
            if error is not None or reminder is None:
                return error or cls._result("not_found", "reminder not found")

            reminder.estado = "eliminado"
            session.commit()
            return cls._result("deleted", "reminder deleted", reminder_id=str(reminder.id))

        except Exception as exc:
            session.rollback()
            print(
                "[REMINDER_DELETE] Persistence error: "
                f"{type(exc).__name__}: {exc}"
            )
            return cls._result("persistence_error", "could not delete reminder")

        finally:
            session.close()

    # ------------------------------------------------------------------
    # Búsqueda por título (match parcial, case-insensitive)
    # ------------------------------------------------------------------

    @classmethod
    def find_by_title(
        cls,
        sender_phone: str,
        title: str,
        estados: set[str] | None = None,
    ) -> tuple["Recordatorio | None", "ReminderResult | None"]:
        """Busca un recordatorio por coincidencia parcial e insensible a mayúsculas.

        Prioriza coincidencia exacta; si no la hay, usa la primera coincidencia
        parcial (title in titulo). Busca entre activos y pausados por defecto.
        """
        sender_phone = cls._normalize_text(sender_phone)
        if not sender_phone:
            return None, cls._result("invalid_data", "sender_phone is required")

        if not title or not str(title).strip():
            return None, cls._result("invalid_data", "title is required")

        search_term = str(title).strip().lower()
        allowed_states = estados or {"activo", "pausado"}

        session = SessionLocal()
        try:
            user = cls._get_user(session, sender_phone)
            if user is None:
                return None, cls._result("user_not_found", "user not found")

            reminders = (
                session.query(Recordatorio)
                .filter(
                    Recordatorio.usuario_id == user.id,
                    Recordatorio.estado.in_(allowed_states),
                )
                .all()
            )

            # Priorizar coincidencia exacta; luego parcial (contains)
            exact: Recordatorio | None = None
            partial: Recordatorio | None = None
            for r in reminders:
                titulo_lower = (r.titulo or "").lower()
                if titulo_lower == search_term:
                    exact = r
                    break
                if search_term in titulo_lower and partial is None:
                    partial = r

            found = exact or partial
            if found is None:
                return None, cls._result(
                    "not_found",
                    f"No encontré un recordatorio con ese nombre.",
                )

            # Expunge para que el caller pueda usar el objeto fuera de la sesión
            from sqlalchemy.orm import make_transient
            session.expunge(found)
            make_transient(found)

            return found, None

        finally:
            session.close()

    @staticmethod
    def title_exists(session, user_id, title: str) -> bool:
        """Retorna True si ya existe un recordatorio no eliminado con ese título (case-insensitive)."""
        search_term = str(title).strip().lower()
        reminders = (
            session.query(Recordatorio)
            .filter(
                Recordatorio.usuario_id == user_id,
                Recordatorio.estado.in_({"activo", "pausado"}),
            )
            .all()
        )
        return any(
            (r.titulo or "").lower() == search_term
            for r in reminders
        )

    # ------------------------------------------------------------------
    # Operaciones por título (sin UUID visible para el usuario)
    # ------------------------------------------------------------------

    @classmethod
    def pause_by_title(cls, sender_phone: str, title: str) -> ReminderResult:
        """Pausa el recordatorio que coincide con el título (parcial, case-insensitive)."""
        reminder, error = cls.find_by_title(sender_phone, title, estados={"activo"})
        if error is not None or reminder is None:
            if error and error.status == "not_found":
                return cls._result("not_found", "No encontré un recordatorio activo con ese nombre.")
            return error or cls._result("not_found", "No encontré un recordatorio activo con ese nombre.")
        return cls.pause_reminder(sender_phone, str(reminder.id))

    @classmethod
    def activate_by_title(cls, sender_phone: str, title: str) -> ReminderResult:
        """Activa el recordatorio pausado que coincide con el título."""
        reminder, error = cls.find_by_title(sender_phone, title, estados={"pausado"})
        if error is not None or reminder is None:
            if error and error.status == "not_found":
                return cls._result("not_found", "No encontré un recordatorio pausado con ese nombre.")
            return error or cls._result("not_found", "No encontré un recordatorio pausado con ese nombre.")
        return cls.activate_reminder(sender_phone, str(reminder.id))

    @classmethod
    def delete_by_title(cls, sender_phone: str, title: str) -> ReminderResult:
        """Elimina (soft-delete) el recordatorio que coincide con el título."""
        reminder, error = cls.find_by_title(sender_phone, title)
        if error is not None or reminder is None:
            return error or cls._result("not_found", "No encontré un recordatorio con ese nombre.")
        return cls.delete_reminder(sender_phone, str(reminder.id))