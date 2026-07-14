import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
from typing import Any, Optional
from uuid import UUID

import matplotlib.pyplot as plt
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    Categoria, MovimientoFinanciero, SessionLocal, Usuario, LimiteCategoria
)


@dataclass
class MovementRegistrationResult:
    status: str
    message: str
    movement_id: str | None = None
    user_id: str | None = None
    duplicate: bool = False


class FinanceService:
    VALID_MOVEMENT_TYPES = {"ingreso", "egreso"}

    # ── Helpers de registro de movimientos ─────────────────────────────

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

    # ── Validación de límites (STK-85) ─────────────────────────────────

    @staticmethod
    def validate_budget_amount(amount: float) -> tuple[bool, str]:
        if amount is None:
            return False, "El monto no puede estar vacío."

        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return False, "El monto debe ser un valor numérico."

        if amount <= 0:
            return False, "El monto debe ser mayor a cero."

        if amount > 9_999_999_999:
            return False, "El monto ingresado es demasiado alto. Ingresá un valor menor."

        return True, ""

    # ── Helpers de límites (STK-86) ───────────────────────────────────

    @staticmethod
    def _obtener_rango_mes(mes: Optional[str] = None) -> tuple[date, date]:
        if not mes:
            hoy = datetime.utcnow()
            mes = hoy.strftime("%Y-%m")

        try:
            year, month = map(int, mes.split("-"))
            inicio = date(year, month, 1)
            if month == 12:
                fin = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                fin = date(year, month + 1, 1) - timedelta(days=1)
            return inicio, fin
        except (ValueError, IndexError):
            raise ValueError(f"Formato de mes inválido: '{mes}'. Debe ser YYYY-MM (ej: 2026-07).")

    @staticmethod
    def _obtener_categoria_por_nombre(db, usuario_id: UUID, nombre_categoria: str) -> Optional[Categoria]:
        categoria = (
            db.query(Categoria)
            .filter(
                Categoria.usuario_id == usuario_id,
                Categoria.nombre.ilike(nombre_categoria),
                Categoria.esta_eliminado == False,
            )
            .first()
        )

        if categoria:
            return categoria

        categoria = (
            db.query(Categoria)
            .filter(
                Categoria.usuario_id.is_(None),
                Categoria.nombre.ilike(nombre_categoria),
                Categoria.es_default == True,
                Categoria.esta_eliminado == False,
            )
            .first()
        )

        return categoria

    # ── CRUD Límites (STK-84, STK-87) ─────────────────────────────────

    @staticmethod
    def set_budget_limit(
        user_id: UUID,
        category: str,
        amount: float,
        month: Optional[str] = None,
    ) -> dict:
        is_valid, error_msg = FinanceService.validate_budget_amount(amount)
        if not is_valid:
            return {"success": False, "message": error_msg}

        try:
            inicio, fin = FinanceService._obtener_rango_mes(month)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        db = SessionLocal()
        try:
            categoria = FinanceService._obtener_categoria_por_nombre(db, user_id, category)
            if not categoria:
                return {
                    "success": False,
                    "message": (
                        f"No encontré una categoría llamada '{category}'. "
                        "Podés usar categorías como: comida, transporte, salidas, etc."
                    ),
                }

            existing = (
                db.query(LimiteCategoria)
                .filter(
                    LimiteCategoria.usuario_id == user_id,
                    LimiteCategoria.categoria_id == categoria.id,
                    LimiteCategoria.inicio_periodo == inicio,
                    LimiteCategoria.fin_periodo == fin,
                )
                .first()
            )

            if existing:
                existing.cantidad_max = amount
                db.commit()
                return {
                    "success": True,
                    "message": (
                        f"✅ Actualicé tu límite de {categoria.nombre} para {inicio.strftime('%Y-%m')} "
                        f"a ${amount:,.0f}."
                    ),
                    "budget_id": str(existing.id),
                }
            else:
                nuevo = LimiteCategoria(
                    usuario_id=user_id,
                    categoria_id=categoria.id,
                    cantidad_max=amount,
                    inicio_periodo=inicio,
                    fin_periodo=fin,
                )
                db.add(nuevo)
                db.commit()
                return {
                    "success": True,
                    "message": (
                        f"✅ Listo. Estableciste un límite de ${amount:,.0f} "
                        f"para {categoria.nombre} en {inicio.strftime('%Y-%m')}."
                    ),
                    "budget_id": str(nuevo.id),
                }
        except Exception as exc:
            db.rollback()
            return {"success": False, "message": f"Error al guardar el límite: {exc}"}
        finally:
            db.close()

    # ── Consulta de límites (STK-88) ──────────────────────────────────

    @staticmethod
    def get_budget_limit(
        user_id: UUID,
        category: str,
        month: Optional[str] = None,
    ) -> dict:
        try:
            inicio, fin = FinanceService._obtener_rango_mes(month)
        except ValueError as e:
            return {"found": False, "message": str(e)}

        db = SessionLocal()
        try:
            categoria = FinanceService._obtener_categoria_por_nombre(db, user_id, category)
            if not categoria:
                return {
                    "found": False,
                    "category": category,
                    "message": f"No encontré una categoría llamada '{category}'.",
                }

            limite = (
                db.query(LimiteCategoria)
                .filter(
                    LimiteCategoria.usuario_id == user_id,
                    LimiteCategoria.categoria_id == categoria.id,
                    LimiteCategoria.inicio_periodo == inicio,
                    LimiteCategoria.fin_periodo == fin,
                )
                .first()
            )

            if limite:
                return {
                    "found": True,
                    "category": categoria.nombre,
                    "amount": limite.cantidad_max,
                    "month": inicio.strftime("%Y-%m"),
                    "message": (
                        f"📊 Tu límite para {categoria.nombre} en {inicio.strftime('%Y-%m')} "
                        f"es de ${limite.cantidad_max:,.0f}."
                    ),
                }
            else:
                return {
                    "found": False,
                    "category": category,
                    "month": inicio.strftime("%Y-%m"),
                    "message": (
                        f"No tenés un límite configurado para {category} en "
                        f"{inicio.strftime('%Y-%m')}. ¿Querés establecer uno?"
                    ),
                }
        except Exception as exc:
            return {"found": False, "message": f"Error al consultar el límite: {exc}"}
        finally:
            db.close()

    @staticmethod
    def get_all_budget_limits(user_id: UUID, month: Optional[str] = None) -> list:
        try:
            inicio, fin = FinanceService._obtener_rango_mes(month)
        except ValueError:
            return []

        db = SessionLocal()
        try:
            limites = (
                db.query(LimiteCategoria)
                .filter(
                    LimiteCategoria.usuario_id == user_id,
                    LimiteCategoria.inicio_periodo == inicio,
                    LimiteCategoria.fin_periodo == fin,
                )
                .all()
            )

            resultados = []
            for lim in limites:
                categoria = db.query(Categoria).filter(Categoria.id == lim.categoria_id).first()
                resultados.append({
                    "id": str(lim.id),
                    "category": categoria.nombre if categoria else "desconocida",
                    "amount": lim.cantidad_max,
                    "month": inicio.strftime("%Y-%m"),
                })
            return resultados
        except Exception:
            return []
        finally:
            db.close()

    @staticmethod
    def check_budget_status(
        user_id: UUID,
        category: str,
        month: Optional[str] = None,
    ) -> dict:
        try:
            inicio, fin = FinanceService._obtener_rango_mes(month)
        except ValueError as e:
            return {"has_limit": False, "message": str(e)}

        db = SessionLocal()
        try:
            categoria = FinanceService._obtener_categoria_por_nombre(db, user_id, category)
            if not categoria:
                return {
                    "has_limit": False,
                    "message": f"No encontré una categoría llamada '{category}'.",
                }

            limite = (
                db.query(LimiteCategoria)
                .filter(
                    LimiteCategoria.usuario_id == user_id,
                    LimiteCategoria.categoria_id == categoria.id,
                    LimiteCategoria.inicio_periodo == inicio,
                    LimiteCategoria.fin_periodo == fin,
                )
                .first()
            )

            if not limite:
                return {
                    "has_limit": False,
                    "category": category,
                    "month": inicio.strftime("%Y-%m"),
                    "message": (
                        f"No hay límite configurado para {category} en "
                        f"{inicio.strftime('%Y-%m')}."
                    ),
                }

            total = (
                db.query(MovimientoFinanciero.cantidad)
                .filter(
                    MovimientoFinanciero.usuario_id == user_id,
                    MovimientoFinanciero.categoria_id == categoria.id,
                    MovimientoFinanciero.tipo == "egreso",
                    MovimientoFinanciero.fecha_movimiento >= inicio,
                    MovimientoFinanciero.fecha_movimiento <= fin,
                )
                .all()
            )

            total_gastado = sum(row[0] for row in total)
            remaining = limite.cantidad_max - total_gastado
            percentage = (total_gastado / limite.cantidad_max) * 100 if limite.cantidad_max > 0 else 0

            return {
                "has_limit": True,
                "category": categoria.nombre,
                "month": inicio.strftime("%Y-%m"),
                "limit": limite.cantidad_max,
                "spent": total_gastado,
                "remaining": remaining,
                "percentage": round(percentage, 1),
                "message": (
                    f"📊 {categoria.nombre} en {inicio.strftime('%Y-%m')}: "
                    f"gastaste ${total_gastado:,.0f} de ${limite.cantidad_max:,.0f} "
                    f"({percentage:.1f}%). Te quedan ${remaining:,.0f}."
                ),
            }
        except Exception as exc:
            return {"has_limit": False, "message": f"Error al consultar estado: {exc}"}
        finally:
            db.close()

    # ── Métodos legacy ────────────────────────────────────────────────

    @staticmethod
    def check_dynamic_budget(user_id: int, new_expense: float, category: str) -> str:
        return "¡Buen registro! Te pasaste un poco en ocio, pero ajustamos el límite de ropa de este mes para que sigas en carrera. ¡Vamos bien!"

    @staticmethod
    def generate_expense_chart(expenses_by_category: dict) -> bytes:
        labels = list(expenses_by_category.keys())
        sizes = list(expenses_by_category.values())

        fig, ax = plt.subplots(figsize=(6, 6))
        ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90)
        ax.axis('equal')

        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)

        plt.close(fig)
        return buf.read()