import matplotlib.pyplot as plt
import io
from datetime import datetime, date, timedelta
from typing import Optional
from uuid import UUID

from app.models.database import SessionLocal, LimiteCategoria, Categoria, MovimientoFinanciero


class FinanceService:
    # ── Validación ───────────────────────────────────────────────────

    @staticmethod
    def validate_budget_amount(amount: float) -> tuple[bool, str]:
        """
        Valida que el monto del límite sea numérico, mayor a cero
        y dentro de rangos permitidos.
        Retorna (es_válido, mensaje_error).
        """
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

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _obtener_rango_mes(mes: Optional[str] = None) -> tuple[date, date]:
        """
        Convierte un string 'YYYY-MM' en (inicio_periodo, fin_periodo).
        Si no se especifica mes, usa el mes actual.
        """
        if not mes:
            hoy = datetime.utcnow()
            mes = hoy.strftime("%Y-%m")

        try:
            year, month = map(int, mes.split("-"))
            inicio = date(year, month, 1)
            # Primer día del mes siguiente - 1 día = último día del mes
            if month == 12:
                fin = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                fin = date(year, month + 1, 1) - timedelta(days=1)
            return inicio, fin
        except (ValueError, IndexError):
            raise ValueError(f"Formato de mes inválido: '{mes}'. Debe ser YYYY-MM (ej: 2026-07).")

    @staticmethod
    def _obtener_categoria_por_nombre(db, usuario_id: UUID, nombre_categoria: str) -> Optional[Categoria]:
        """
        Busca una categoría por nombre para el usuario. Si no la encuentra,
        busca en las categorías default.
        """
        # Buscar categoría del usuario
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

        # Buscar en categorías default (sin usuario_id específico)
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

    # ── CRUD Límites ─────────────────────────────────────────────────

    @staticmethod
    def set_budget_limit(
        user_id: UUID,
        category: str,
        amount: float,
        month: Optional[str] = None,
    ) -> dict:
        """
        Crea o actualiza el límite mensual para una categoría en `limite_categoria`.
        Busca la categoría por nombre y la vincula por UUID.
        Si no se especifica mes, se usa el mes actual.
        """
        # Validar monto
        is_valid, error_msg = FinanceService.validate_budget_amount(amount)
        if not is_valid:
            return {"success": False, "message": error_msg}

        # Calcular período
        try:
            inicio, fin = FinanceService._obtener_rango_mes(month)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        db = SessionLocal()
        try:
            # Buscar categoría por nombre
            categoria = FinanceService._obtener_categoria_por_nombre(db, user_id, category)
            if not categoria:
                return {
                    "success": False,
                    "message": (
                        f"No encontré una categoría llamada '{category}'. "
                        "Podés usar categorías como: comida, transporte, salidas, etc."
                    ),
                }

            # Buscar si ya existe un límite para (usuario, categoría, período)
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

    @staticmethod
    def get_budget_limit(
        user_id: UUID,
        category: str,
        month: Optional[str] = None,
    ) -> dict:
        """
        Obtiene el límite configurado para una categoría en un mes específico.
        """
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
        """
        Obtiene todos los límites configurados para un usuario en un mes.
        """
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
        """
        Calcula el progreso del gasto vs el límite para una categoría en un mes.
        """
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

            # Obtener límite
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

            # Calcular gasto total del período para esa categoría
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

    # ── Métodos existentes ───────────────────────────────────────────

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
        Genera un gráfico de torta básico y lo retorna como bytes.
        """
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