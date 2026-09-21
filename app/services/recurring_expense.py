"""Servicio determinista de detección de gastos recurrentes mensuales.

Implementa STK-186 (subtarea de STK-43 / HU-REM-03).
Examina egresos vigentes en una ventana histórica acotada, detecta patrones
en 3 meses calendario consecutivos con tolerancia de fechas, calcula la próxima
ocurrencia estimada y persiste candidatos de forma idempotente con trazabilidad.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import re
import time
from typing import Any
import unicodedata
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.database import (
    CandidatoGastoRecurrente,
    MovimientoFinanciero,
    Recordatorio,
)

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

NON_INFORMATIVE_TERMS = frozenset({
    "gasto", "gastos",
    "pago", "pagos",
    "compra", "compras",
    "varios", "varias",
    "otro", "otros", "otra", "otras",
    "consumo", "consumos",
    "movimiento", "movimientos",
})


def normalize_description(raw: Any) -> str | None:
    """Normaliza determinísticamente una descripción de gasto.

    Transformaciones:
    1. Descomposición Unicode NFKD y eliminación de marcas diacríticas/acentos.
    2. Conversión a minúsculas.
    3. Reemplazo de caracteres no alfanuméricos por espacios.
    4. Colapso de espacios múltiples y strip.
    5. Descarte de no informativos: vacío, sin letras (solo números/símbolos),
       longitud menor a 2 caracteres o términos genéricos sin concepto.
    """
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    alphanumeric = re.sub(r"[^a-z0-9]+", " ", stripped)
    normalized = " ".join(alphanumeric.split())

    if not normalized:
        return None

    if len(normalized) < 2:
        return None

    # Requiere al menos un carácter alfabético (descarta solo números como "12345")
    if not re.search(r"[a-z]", normalized):
        return None

    if normalized in NON_INFORMATIVE_TERMS:
        return None

    return normalized


def calculate_pattern_hash(
    norm_desc: str,
    categoria_id: UUID | None,
    moneda: str,
) -> str:
    """Calcula un SHA-256 canónico y estable para la clave del patrón recurrente."""
    cat_str = str(categoria_id) if categoria_id else "none"
    curr_str = (moneda or "ARS").upper().strip()
    payload = f"{norm_desc}|{cat_str}|{curr_str}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def calculate_next_occurrence(
    last_date: date,
    target_day: int | None = None,
    as_of_date: date | None = None,
) -> date:
    """Calcula la próxima fecha mensual estimada a partir de una fecha y día objetivo.

    Reglas:
    - Avanza al próximo mes calendario.
    - Maneja el cambio de año (diciembre -> enero).
    - Para días 29, 30 o 31, ajusta la proyección al último día válido del mes siguiente.
    - Garantiza que la fecha resultante sea estrictamente posterior a as_of_date (si se indica),
      avanzando los meses necesarios con el mismo día nominal y ajuste de fin de mes.
    """
    anchor_day = last_date.day if target_day is None else int(target_day)
    anchor_day = max(1, min(31, anchor_day))

    if last_date.month == 12:
        curr_year = last_date.year + 1
        curr_month = 1
    else:
        curr_year = last_date.year
        curr_month = last_date.month + 1

    days_in_month = calendar.monthrange(curr_year, curr_month)[1]
    projected_date = date(curr_year, curr_month, min(anchor_day, days_in_month))

    if as_of_date is not None:
        while projected_date <= as_of_date:
            if curr_month == 12:
                curr_year += 1
                curr_month = 1
            else:
                curr_month += 1
            days_in_month = calendar.monthrange(curr_year, curr_month)[1]
            projected_date = date(curr_year, curr_month, min(anchor_day, days_in_month))

    return projected_date


def _next_calendar_month(ym: tuple[int, int]) -> tuple[int, int]:
    year, month = ym
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _are_consecutive_months(
    m1: tuple[int, int],
    m2: tuple[int, int],
    m3: tuple[int, int],
) -> bool:
    return _next_calendar_month(m1) == m2 and _next_calendar_month(m2) == m3


def _matches_date_tolerance(
    d1: int,
    l1: int,
    d2: int,
    l2: int,
    d3: int,
    l3: int,
    tolerance_days: int = 3,
) -> bool:
    """Verifica si 3 días de meses consecutivos caen dentro de la tolerancia.

    Considera:
    1. Tolerancia respecto a la mediana de días de mes.
    2. Tolerancia de fin de mes si alguno de los días es >= 28.
    """
    sorted_days = sorted([d1, d2, d3])
    median_day = sorted_days[1]
    if (
        abs(d1 - median_day) <= tolerance_days
        and abs(d2 - median_day) <= tolerance_days
        and abs(d3 - median_day) <= tolerance_days
    ):
        return True

    # Fin de mes: evalúa la distancia relativa al último día de cada mes
    if d1 >= 28 or d2 >= 28 or d3 >= 28:
        e1, e2, e3 = l1 - d1, l2 - d2, l3 - d3
        sorted_e = sorted([e1, e2, e3])
        median_e = sorted_e[1]
        if (
            abs(e1 - median_e) <= tolerance_days
            and abs(e2 - median_e) <= tolerance_days
            and abs(e3 - median_e) <= tolerance_days
        ):
            return True

    return False


def _find_recent_recurring_evidence(
    movements: list[MovimientoFinanciero],
    tolerance_days: int = 3,
) -> list[MovimientoFinanciero] | None:
    """Busca la terna más reciente de movimientos que cumpla 3 meses consecutivos dentro de tolerancia.

    Aplica deduplicación por día dentro de cada mes y acotamiento de candidatos para evitar
    el producto cartesiano sin límite.
    """
    if len(movements) < 3:
        return None

    # Agrupar por mes calendario (año, mes)
    by_month: dict[tuple[int, int], list[MovimientoFinanciero]] = {}
    for m in movements:
        ym = (m.fecha_movimiento.year, m.fecha_movimiento.month)
        by_month.setdefault(ym, []).append(m)

    sorted_months = sorted(by_month.keys())
    if len(sorted_months) < 3:
        return None

    # Evaluar ternas de meses de forma inversa (de la más reciente a la más antigua)
    for i in range(len(sorted_months) - 1, 1, -1):
        m3_ym = sorted_months[i]
        m2_ym = sorted_months[i - 1]
        m1_ym = sorted_months[i - 2]

        if not _are_consecutive_months(m1_ym, m2_ym, m3_ym):
            continue

        l1 = calendar.monthrange(m1_ym[0], m1_ym[1])[1]
        l2 = calendar.monthrange(m2_ym[0], m2_ym[1])[1]
        l3 = calendar.monthrange(m3_ym[0], m3_ym[1])[1]

        # Deduplicar por día dentro de cada mes (máximo 31 días por mes)
        m3_by_day: dict[int, MovimientoFinanciero] = {m.fecha_movimiento.day: m for m in by_month[m3_ym]}
        m2_by_day: dict[int, MovimientoFinanciero] = {m.fecha_movimiento.day: m for m in by_month[m2_ym]}
        m1_by_day: dict[int, MovimientoFinanciero] = {m.fecha_movimiento.day: m for m in by_month[m1_ym]}

        days_3 = sorted(m3_by_day.keys(), reverse=True)
        days_2 = sorted(m2_by_day.keys(), reverse=True)
        days_1 = sorted(m1_by_day.keys(), reverse=True)

        for d3 in days_3:
            mov3 = m3_by_day[d3]
            # Filtrar días de m2 y m1 dentro de una ventana acotada de tolerancia
            cand_days_2 = [
                d2 for d2 in days_2
                if abs(d2 - d3) <= 2 * tolerance_days
                or (d3 >= 28 and d2 >= 28 and abs((l3 - d3) - (l2 - d2)) <= 2 * tolerance_days)
            ]
            for d2 in cand_days_2:
                mov2 = m2_by_day[d2]
                cand_days_1 = [
                    d1 for d1 in days_1
                    if abs(d1 - d3) <= 2 * tolerance_days
                    or (d3 >= 28 and d1 >= 28 and abs((l3 - d3) - (l1 - d1)) <= 2 * tolerance_days)
                ]
                for d1 in cand_days_1:
                    mov1 = m1_by_day[d1]
                    if _matches_date_tolerance(d1, l1, d2, l2, d3, l3, tolerance_days):
                        return [mov1, mov2, mov3]

    return None


def _calculate_start_date(as_of: date, lookback_months: int = 4) -> date:
    """Calcula la fecha de inicio de la ventana histórica acotada."""
    year = as_of.year
    month = as_of.month - (lookback_months - 1)
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, 1)


@dataclass
class RecurringDetectionMetrics:
    duration_ms: float = 0.0
    rows_examined: int = 0
    users_processed: int = 0
    patterns_evaluated: int = 0
    candidates_created: int = 0
    candidates_updated: int = 0
    candidates_unchanged: int = 0
    candidates_invalidated: int = 0
    candidates_discarded: int = 0


@dataclass
class RecurringDetectionResult:
    status: str
    dry_run: bool
    metrics: RecurringDetectionMetrics
    candidates: list[CandidatoGastoRecurrente] = field(default_factory=list)


class RecurringExpenseService:
    """Servicio de detección batch y gestión de candidatos de gastos recurrentes."""

    @classmethod
    def detect_candidates(
        cls,
        session: Session,
        user_id: UUID | None = None,
        as_of_date: date | None = None,
        tolerance_days: int = 3,
        dry_run: bool = False,
        lookback_months: int = 4,
        batch_size: int = 500,
    ) -> RecurringDetectionResult:
        """Ejecuta la detección de candidatos de gastos recurrentes.

        Examina egresos no anulados en la ventana acotada [start_date, as_of_date],
        agrupa por patrón determinista e identifica evidencia en 3 meses consecutivos.
        Persiste candidatos de manera idempotente y concurrencialmente segura,
        o reporta en modo dry-run sin mutar el estado de la sesión.
        """
        if lookback_months < 3:
            raise ValueError("lookback_months debe ser mayor o igual a 3")
        if tolerance_days < 0:
            raise ValueError("tolerance_days debe ser mayor o igual a 0")
        if batch_size < 1:
            raise ValueError("batch_size debe ser mayor o igual a 1")

        start_time = time.perf_counter()
        reference_date = as_of_date or datetime.now(ARGENTINA_TZ).date()
        start_date = _calculate_start_date(reference_date, lookback_months=lookback_months)

        metrics = RecurringDetectionMetrics()

        # 1. Precargar todos los candidatos existentes en UNA consulta constante (sin N+1)
        candidates_query = select(CandidatoGastoRecurrente)
        if user_id is not None:
            candidates_query = candidates_query.where(CandidatoGastoRecurrente.usuario_id == user_id)

        existing_candidates_rows = session.execute(candidates_query).scalars().all()
        existing_by_user_and_hash: dict[tuple[UUID, str], CandidatoGastoRecurrente] = {
            (c.usuario_id, c.patron_hash): c for c in existing_candidates_rows
        }
        existing_by_user: dict[UUID, list[CandidatoGastoRecurrente]] = {}
        for c in existing_candidates_rows:
            existing_by_user.setdefault(c.usuario_id, []).append(c)

        # 2. Streaming de movimientos sin .all() ordenado por (usuario_id, fecha_movimiento, id)
        stream_query = (
            select(MovimientoFinanciero)
            .where(
                MovimientoFinanciero.tipo == "egreso",
                MovimientoFinanciero.anulado_en.is_(None),
                MovimientoFinanciero.fecha_movimiento >= start_date,
                MovimientoFinanciero.fecha_movimiento <= reference_date,
                MovimientoFinanciero.descripcion.isnot(None),
            )
            .order_by(
                MovimientoFinanciero.usuario_id,
                MovimientoFinanciero.fecha_movimiento.asc(),
                MovimientoFinanciero.id.asc(),
            )
        )

        if user_id is not None:
            stream_query = stream_query.where(MovimientoFinanciero.usuario_id == user_id)

        result_stream = session.execute(
            stream_query.execution_options(yield_per=batch_size)
        ).scalars()

        result_candidates: list[CandidatoGastoRecurrente] = []
        processed_users: set[UUID] = set()

        def _process_user(u_id: UUID, user_movements: list[MovimientoFinanciero]) -> None:
            nonlocal metrics
            metrics.users_processed += 1
            processed_users.add(u_id)

            user_existing_list = existing_by_user.get(u_id, [])
            user_existing_by_hash: dict[str, CandidatoGastoRecurrente] = {
                c.patron_hash: c for c in user_existing_list
            }

            # Agrupar movimientos de este usuario por patrón determinista
            patterns: dict[str, tuple[str, UUID | None, str, str, list[MovimientoFinanciero]]] = {}
            for mov in user_movements:
                norm_desc = normalize_description(mov.descripcion)
                if norm_desc is None:
                    continue

                curr = (mov.moneda or "ARS").upper().strip()
                p_hash = calculate_pattern_hash(norm_desc, mov.categoria_id, curr)
                if p_hash not in patterns:
                    patterns[p_hash] = (norm_desc, mov.categoria_id, curr, str(mov.descripcion).strip(), [])
                patterns[p_hash][4].append(mov)

            detected_hashes_for_user: set[str] = set()

            for p_hash, (norm_desc, cat_id, curr, latest_desc, mov_list) in patterns.items():
                metrics.patterns_evaluated += 1

                evidence = _find_recent_recurring_evidence(mov_list, tolerance_days=tolerance_days)
                if evidence is None:
                    metrics.candidates_discarded += 1
                    continue

                detected_hashes_for_user.add(p_hash)
                m1, m2, m3 = evidence
                last_mov_date = m3.fecha_movimiento
                concept = str(m3.descripcion).strip() or latest_desc
                dia_estimado = last_mov_date.day

                # Fin de mes: conservar 31 si alguno ocurrió el día 31 o fin de mes
                l3 = calendar.monthrange(last_mov_date.year, last_mov_date.month)[1]
                if last_mov_date.day == l3 and any(m.fecha_movimiento.day == 31 for m in (m1, m2, m3)):
                    dia_estimado = 31

                # Proyección estrictamente posterior a reference_date
                proxima_fecha = calculate_next_occurrence(
                    last_mov_date,
                    target_day=dia_estimado,
                    as_of_date=reference_date,
                )
                evidence_ids = [str(m.id) for m in evidence]
                estimated_amount = Decimal(str(m3.cantidad)) if m3.cantidad is not None else None

                existing = user_existing_by_hash.get(p_hash)

                if existing is None:
                    candidate = CandidatoGastoRecurrente(
                        usuario_id=u_id,
                        patron_hash=p_hash,
                        descripcion_normalizada=norm_desc,
                        categoria_id=cat_id,
                        moneda=curr,
                        concepto=concept,
                        monto_estimado=estimated_amount,
                        dia_estimado=dia_estimado,
                        proxima_fecha_estimada=proxima_fecha,
                        estado="pendiente",
                        ultima_fecha_movimiento=last_mov_date,
                        evidencia_movimiento_ids=evidence_ids,
                    )
                    if dry_run:
                        metrics.candidates_created += 1
                        result_candidates.append(candidate)
                    else:
                        # Persistencia segura ante concurrencia mediante savepoint
                        try:
                            with session.begin_nested():
                                session.add(candidate)
                                session.flush()
                            metrics.candidates_created += 1
                            result_candidates.append(candidate)
                            user_existing_by_hash[p_hash] = candidate
                            existing_by_user_and_hash[(u_id, p_hash)] = candidate
                        except IntegrityError as exc:
                            # Carrera con otro worker: recuperar registro ya insertado
                            race_cand = session.execute(
                                select(CandidatoGastoRecurrente).where(
                                    CandidatoGastoRecurrente.usuario_id == u_id,
                                    CandidatoGastoRecurrente.patron_hash == p_hash,
                                )
                            ).scalar_one_or_none()
                            if race_cand is not None:
                                if last_mov_date > race_cand.ultima_fecha_movimiento:
                                    race_cand.proxima_fecha_estimada = proxima_fecha
                                    race_cand.dia_estimado = dia_estimado
                                    race_cand.monto_estimado = estimated_amount
                                    race_cand.concepto = concept
                                    race_cand.ultima_fecha_movimiento = last_mov_date
                                    race_cand.evidencia_movimiento_ids = evidence_ids
                                    if race_cand.estado == "invalidado":
                                        race_cand.estado = "pendiente"
                                    race_cand.actualizado_en = datetime.now(timezone.utc)
                                    metrics.candidates_updated += 1
                                    result_candidates.append(race_cand)
                                else:
                                    metrics.candidates_unchanged += 1
                                    result_candidates.append(race_cand)
                            else:
                                raise RuntimeError(
                                    f"Colisión de integridad al persistir candidato para usuario {u_id} "
                                    f"y patrón {p_hash}, pero el registro concurrente no fue encontrado en la base de datos."
                                ) from exc
                else:
                    if last_mov_date > existing.ultima_fecha_movimiento or existing.estado == "invalidado":
                        if dry_run:
                            # En dry-run retornar proyección nueva completa sin mutar el objeto persistido ni la sesión
                            simulated = CandidatoGastoRecurrente(
                                id=existing.id,
                                usuario_id=existing.usuario_id,
                                patron_hash=existing.patron_hash,
                                descripcion_normalizada=existing.descripcion_normalizada,
                                categoria_id=existing.categoria_id,
                                moneda=existing.moneda,
                                concepto=concept,
                                monto_estimado=estimated_amount,
                                dia_estimado=dia_estimado,
                                proxima_fecha_estimada=proxima_fecha,
                                estado="pendiente" if existing.estado == "invalidado" else existing.estado,
                                ultima_fecha_movimiento=last_mov_date,
                                evidencia_movimiento_ids=evidence_ids,
                                creado_en=existing.creado_en,
                                actualizado_en=datetime.now(timezone.utc),
                            )
                            metrics.candidates_updated += 1
                            result_candidates.append(simulated)
                        else:
                            existing.proxima_fecha_estimada = proxima_fecha
                            existing.dia_estimado = dia_estimado
                            existing.monto_estimado = estimated_amount
                            existing.concepto = concept
                            existing.ultima_fecha_movimiento = last_mov_date
                            existing.evidencia_movimiento_ids = evidence_ids
                            if existing.estado == "invalidado":
                                existing.estado = "pendiente"
                            existing.actualizado_en = datetime.now(timezone.utc)
                            metrics.candidates_updated += 1
                            result_candidates.append(existing)
                    else:
                        metrics.candidates_unchanged += 1
                        result_candidates.append(existing)

            # 3. Invalidar candidatos pendientes que perdieron evidencia válida activa
            for cand in user_existing_list:
                if cand.estado == "pendiente" and cand.patron_hash not in detected_hashes_for_user:
                    if not dry_run:
                        cand.estado = "invalidado"
                        cand.actualizado_en = datetime.now(timezone.utc)
                    metrics.candidates_invalidated += 1

        # Iterar el stream agrupando por usuario respetando los límites de lote
        current_u_id: UUID | None = None
        current_user_movs: list[MovimientoFinanciero] = []

        for mov in result_stream:
            metrics.rows_examined += 1
            if current_u_id is None:
                current_u_id = mov.usuario_id
            elif mov.usuario_id != current_u_id:
                _process_user(current_u_id, current_user_movs)
                current_u_id = mov.usuario_id
                current_user_movs = []
            current_user_movs.append(mov)

        if current_u_id is not None and current_user_movs:
            _process_user(current_u_id, current_user_movs)

        # 4. Usuarios con candidatos pendientes previos que no tuvieron ningún movimiento en la ventana
        for u_id, c_list in existing_by_user.items():
            if u_id not in processed_users:
                metrics.users_processed += 1
                for c in c_list:
                    if c.estado == "pendiente":
                        if not dry_run:
                            c.estado = "invalidado"
                            c.actualizado_en = datetime.now(timezone.utc)
                        metrics.candidates_invalidated += 1

        if not dry_run:
            session.commit()

        metrics.duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return RecurringDetectionResult(
            status="ok",
            dry_run=dry_run,
            metrics=metrics,
            candidates=result_candidates,
        )

    @classmethod
    def convert_candidate_to_reminder(
        cls,
        session: Session,
        user_id: UUID,
        candidate_id: UUID,
    ) -> tuple[str, Recordatorio | None]:
        """Convierte atómicamente un candidato a recordatorio con anticipación de 3 días.

        Garantiza idempotencia y seguridad concurrente:
        1. Validación estricta de candidate_id y user_id.
        2. Validación de expiración de la propuesta (ventana de 7 días o vencimiento).
        3. Detección de estados previos (aceptado, rechazado, pausado, desactivado, invalidado).
        4. Transición atómica condicional sobre estado='pendiente'.
        5. Inserción de Recordatorio protegido por UNIQUE(candidato_id).
        6. Recuperación segura ante IntegrityError concurrente.
        """
        cand_any = session.execute(
            select(CandidatoGastoRecurrente).where(CandidatoGastoRecurrente.id == candidate_id)
        ).scalar_one_or_none()
        if cand_any is None:
            return "not_found", None
        if cand_any.usuario_id != user_id:
            return "not_found", None

        candidate = cand_any
        if candidate.estado == "aceptado":
            existing_rec = session.execute(
                select(Recordatorio).where(Recordatorio.candidato_id == candidate.id)
            ).scalar_one_or_none()
            return "already_converted", existing_rec

        if candidate.estado == "rechazado":
            return "already_rejected", None

        if candidate.estado in ("pausado", "desactivado", "invalidado"):
            return "unavailable", None

        if candidate.estado != "pendiente":
            return "unavailable", None

        if cls.is_proposal_expired(candidate):
            return "expired", None

        now_utc = datetime.now(timezone.utc)
        title = (candidate.concepto or candidate.descripcion_normalizada or "Gasto recurrente")[:100]

        # Transición atómica condicional a 'aceptado'
        claim_stmt = (
            update(CandidatoGastoRecurrente)
            .where(
                CandidatoGastoRecurrente.id == candidate_id,
                CandidatoGastoRecurrente.usuario_id == user_id,
                CandidatoGastoRecurrente.estado == "pendiente",
            )
            .values(
                estado="aceptado",
                decision_en=now_utc,
                decision_origen="interactivo",
                actualizado_en=now_utc,
            )
        )
        res = session.execute(claim_stmt)
        if res.rowcount != 1:
            session.expire_all()
            reloaded = session.execute(
                select(CandidatoGastoRecurrente).where(CandidatoGastoRecurrente.id == candidate_id)
            ).scalar_one_or_none()
            if not reloaded:
                return "not_found", None
            if reloaded.estado == "aceptado":
                existing_rec = session.execute(
                    select(Recordatorio).where(Recordatorio.candidato_id == candidate.id)
                ).scalar_one_or_none()
                return "already_converted", existing_rec
            if reloaded.estado == "rechazado":
                return "already_rejected", None
            return "conflict", None

        recordatorio = Recordatorio(
            usuario_id=user_id,
            candidato_id=candidate.id,
            titulo=title,
            dia_del_mes=candidate.dia_estimado,
            monto=candidate.monto_estimado,
            moneda=candidate.moneda or "ARS",
            estado="activo",
            dias_anticipacion=3,
            origen="recurrente_inteligente",
        )

        try:
            with session.begin_nested():
                session.add(recordatorio)
                session.flush()
            session.commit()
            return "converted", recordatorio
        except IntegrityError:
            session.rollback()
            existing_rec = session.execute(
                select(Recordatorio).where(Recordatorio.candidato_id == candidate.id)
            ).scalar_one_or_none()
            return "already_converted", existing_rec

    @classmethod
    def reject_candidate(
        cls,
        session: Session,
        user_id: UUID,
        candidate_id: UUID,
    ) -> tuple[str, CandidatoGastoRecurrente | None]:
        """Marca un candidato como rechazado por decisión explícita del usuario."""
        cand_any = session.execute(
            select(CandidatoGastoRecurrente).where(CandidatoGastoRecurrente.id == candidate_id)
        ).scalar_one_or_none()
        if cand_any is None:
            return "not_found", None
        if cand_any.usuario_id != user_id:
            return "not_found", None

        candidate = cand_any
        if candidate.estado == "rechazado":
            return "already_rejected", candidate

        if candidate.estado in ("aceptado", "pausado", "desactivado", "invalidado"):
            return "conflict", candidate

        if candidate.estado != "pendiente":
            return "unavailable", None

        if cls.is_proposal_expired(candidate):
            return "expired", None

        now_utc = datetime.now(timezone.utc)
        stmt = (
            update(CandidatoGastoRecurrente)
            .where(
                CandidatoGastoRecurrente.id == candidate_id,
                CandidatoGastoRecurrente.usuario_id == user_id,
                CandidatoGastoRecurrente.estado == "pendiente",
            )
            .values(
                estado="rechazado",
                decision_en=now_utc,
                decision_origen="interactivo",
                actualizado_en=now_utc,
            )
        )
        res = session.execute(stmt)
        if res.rowcount != 1:
            session.expire_all()
            reloaded = session.execute(
                select(CandidatoGastoRecurrente).where(CandidatoGastoRecurrente.id == candidate_id)
            ).scalar_one_or_none()
            if not reloaded:
                return "not_found", None
            if reloaded.estado == "rechazado":
                return "already_rejected", reloaded
            return "conflict", reloaded

        session.commit()
        session.refresh(candidate)
        return "rejected", candidate

    @staticmethod
    def is_candidate_eligible_for_proposal(
        candidate: CandidatoGastoRecurrente,
        as_of: datetime | None = None,
    ) -> bool:
        """Evalúa si un candidato califica para ser propuesto al usuario.

        Reglas:
        - Estado debe ser 'pendiente'.
        - Cooldown: si propuesta_en no es None, deben haber transcurrido al menos 30 días (en UTC).
        - La próxima fecha estimada no debe haber vencido contra la fecha calendario en Argentina.
        """
        if candidate.estado != "pendiente":
            return False

        now_utc = as_of or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)

        if candidate.propuesta_en is not None:
            prop_en = candidate.propuesta_en
            if prop_en.tzinfo is None:
                prop_en = prop_en.replace(tzinfo=timezone.utc)
            else:
                prop_en = prop_en.astimezone(timezone.utc)
            if (now_utc - prop_en).total_seconds() < 30 * 86400:
                return False

        local_today = now_utc.astimezone(ARGENTINA_TZ).date()
        if candidate.proxima_fecha_estimada < local_today:
            return False

        return True

    @staticmethod
    def is_proposal_expired(
        candidate: CandidatoGastoRecurrente,
        as_of: datetime | None = None,
    ) -> bool:
        """Verifica si la propuesta interactiva ha expirado (7 días en UTC o fecha estimada pasada en Argentina)."""
        now_utc = as_of or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)

        if candidate.propuesta_en is not None:
            prop_en = candidate.propuesta_en
            if prop_en.tzinfo is None:
                prop_en = prop_en.replace(tzinfo=timezone.utc)
            else:
                prop_en = prop_en.astimezone(timezone.utc)
            if (now_utc - prop_en).total_seconds() > 7 * 86400:
                return True

        local_today = now_utc.astimezone(ARGENTINA_TZ).date()
        if candidate.proxima_fecha_estimada and candidate.proxima_fecha_estimada < local_today:
            return True

        return False

    @classmethod
    def check_period_expense_registered(
        cls,
        session: Session,
        user_id: UUID,
        patron_hash: str,
        reference_date: date,
    ) -> bool:
        """Verifica si existe un egreso registrado y no anulado en el mes calendario de reference_date.

        Retorna True si al menos un movimiento del usuario coincide con patron_hash.
        """
        start_date = date(reference_date.year, reference_date.month, 1)
        last_day = calendar.monthrange(reference_date.year, reference_date.month)[1]
        end_date = date(reference_date.year, reference_date.month, last_day)

        query = (
            select(MovimientoFinanciero)
            .where(
                MovimientoFinanciero.usuario_id == user_id,
                MovimientoFinanciero.tipo == "egreso",
                MovimientoFinanciero.anulado_en.is_(None),
                MovimientoFinanciero.fecha_movimiento >= start_date,
                MovimientoFinanciero.fecha_movimiento <= end_date,
                MovimientoFinanciero.descripcion.isnot(None),
            )
        )
        movements = session.execute(query).scalars().all()
        for mov in movements:
            norm = normalize_description(mov.descripcion)
            if norm is None:
                continue
            curr = (mov.moneda or "ARS").upper().strip()
            if calculate_pattern_hash(norm, mov.categoria_id, curr) == patron_hash:
                return True

        return False

    @classmethod
    def run_daily_detection(
        cls,
        session: Session,
        as_of_date: date | None = None,
    ) -> RecurringDetectionResult:
        """Ejecuta la detección batch diaria de candidatos para todos los usuarios."""
        effective_date = as_of_date or datetime.now(ARGENTINA_TZ).date()
        return cls.detect_candidates(session=session, as_of_date=effective_date)
