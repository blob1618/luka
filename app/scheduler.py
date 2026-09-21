import asyncio
import calendar
import logging
import os
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, exists, or_, text, update
from sqlalchemy.exc import IntegrityError

from app.api.whatsapp import (
    WhatsAppDeliveryStatus,
    WhatsAppSendResult,
    send_whatsapp_message,
    send_whatsapp_message_detailed,
)
from app.models.database import (
    AvisoRecordatorio,
    CandidatoGastoRecurrente,
    CronJobClaim,
    MovimientoFinanciero,
    Recordatorio,
    SessionLocal,
    Usuario,
)
from app.services.recurring_expense import RecurringExpenseService

scheduler = AsyncIOScheduler()
logger = logging.getLogger(__name__)

WHATSAPP_WINDOW_HOURS = 24
ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
PROACTIVE_PROMPT_TEXT = "👋 ¡Ey! ¿Tuviste algún gasto hoy que no registraste? Contame y lo anoto. (Si no querés estos avisos, pedime que no te escriba más.)"


def reconcile_stranded_sending_claims(
    session,
    now_utc: datetime,
    cutoff_minutes: int = 15,
) -> int:
    """Reconcilia filas en estado 'sending' que quedaron varadas por caída o crash.

    Si un aviso quedó en 'sending' y su ultimo_intento_en es anterior a now_utc - cutoff_minutes,
    se transiciona a 'unknown' con es_reintentable=False. Esto previene dobles envíos y garantiza entrega at-most-once.
    """
    cutoff = now_utc - timedelta(minutes=cutoff_minutes)
    updated = (
        session.query(AvisoRecordatorio)
        .filter(
            AvisoRecordatorio.estado == "sending",
            AvisoRecordatorio.ultimo_intento_en < cutoff,
        )
        .update(
            {
                AvisoRecordatorio.estado: "unknown",
                AvisoRecordatorio.es_reintentable: False,
                AvisoRecordatorio.error_detalle: "stranded_sending_reconciled",
                AvisoRecordatorio.actualizado_en: now_utc,
            },
            synchronize_session=False,
        )
    )
    if updated > 0:
        session.commit()
        logger.warning(
            "[RECONCILE_STRANDED] Reconciliadas %d filas varadas en 'sending' a 'unknown'",
            updated,
        )
    return updated


def _to_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _window_open(usuario, now_utc: datetime) -> bool:
    last_message_at = _to_aware_utc(usuario.ultimo_mensaje_en)
    return (
        last_message_at is not None
        and last_message_at + timedelta(hours=WHATSAPP_WINDOW_HOURS) > now_utc
    )


def _proactive_hour() -> int | None:
    raw = (os.getenv("PROACTIVE_PROMPT_HOUR") or "").strip()
    if not raw:
        return None
    if raw.isdecimal() and int(raw) <= 23:
        return int(raw)
    logger.warning(
        "[PROACTIVE_CONFIG_ERROR] PROACTIVE_PROMPT_HOUR=%r no es una hora 0-23; aviso apagado",
        raw,
    )
    return None


# ponytail: claim antes de enviar, release si falla; sin outbox, un crash entre claim y envío pierde el aviso del día
def _claim_proactive_send(session, usuario_id, today: date) -> bool:
    updated = (
        session.query(Usuario)
        .filter(
            Usuario.id == usuario_id,
            or_(
                Usuario.proactivo_ultimo_envio.is_(None),
                Usuario.proactivo_ultimo_envio != today,
            ),
        )
        .update({Usuario.proactivo_ultimo_envio: today}, synchronize_session=False)
    )
    session.commit()
    return updated == 1


def _release_proactive_claim(session, usuario_id, today: date):
    try:
        session.query(Usuario).filter(
            Usuario.id == usuario_id,
            Usuario.proactivo_ultimo_envio == today,
        ).update({Usuario.proactivo_ultimo_envio: None}, synchronize_session=False)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            "[PROACTIVE_ERROR] release user=%s %s: %s",
            usuario_id, type(exc).__name__, exc,
        )


def _build_due_datetime(due_date: date) -> datetime:
    return datetime(
        due_date.year,
        due_date.month,
        due_date.day,
        tzinfo=ARGENTINA_TZ,
    )


def _format_amount(amount) -> str:
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return str(amount)

    if decimal_amount == decimal_amount.to_integral_value():
        return str(decimal_amount.quantize(Decimal("1")))
    return str(decimal_amount.normalize())


def _alert_day(dia_del_mes: int, reference_date: date, dias_anticipacion: int = 1) -> date:
    """Calcula el día en que se debe emitir el aviso según los días de anticipación.

    Maneja cambio de mes y año, meses cortos y conserva el comportamiento de HU-REM-01 por defecto (dias_anticipacion=1).
    """
    year = reference_date.year
    month = reference_date.month

    # 1. Vencimiento este mes (ajustado por fin de mes)
    max_day = calendar.monthrange(year, month)[1]
    effective_day = min(dia_del_mes, max_day)
    due_this_month = date(year, month, effective_day)

    # 2. Si el vencimiento ya pasó o es hoy, evaluar próximo mes
    if due_this_month <= reference_date:
        next_month = month + 1
        next_year = year
        if next_month > 12:
            next_month = 1
            next_year += 1
        max_day_next = calendar.monthrange(next_year, next_month)[1]
        effective_day_next = min(dia_del_mes, max_day_next)
        next_due = date(next_year, next_month, effective_day_next)
    else:
        next_due = due_this_month

    # 3. Día de alerta es la fecha de vencimiento menos los días de anticipación
    alert_day = next_due - timedelta(days=dias_anticipacion)

    if alert_day < reference_date and next_due == due_this_month:
        next_month = month + 1
        next_year = year
        if next_month > 12:
            next_month = 1
            next_year += 1
        max_day_next = calendar.monthrange(next_year, next_month)[1]
        effective_day_next = min(dia_del_mes, max_day_next)
        next_due = date(next_year, next_month, effective_day_next)
        alert_day = next_due - timedelta(days=dias_anticipacion)

    return alert_day


def _due_date_for_reminder(dia_del_mes: int, today: date, dias_anticipacion: int = 1) -> date:
    """Calcula la fecha de vencimiento asociada a la alerta actual."""
    max_day = calendar.monthrange(today.year, today.month)[1]
    due_date = date(today.year, today.month, min(dia_del_mes, max_day))
    if due_date <= today or (due_date - timedelta(days=dias_anticipacion)) < today:
        next_month = today.month + 1
        next_year = today.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        max_day_next = calendar.monthrange(next_year, next_month)[1]
        return date(next_year, next_month, min(dia_del_mes, max_day_next))

    return due_date


def _build_message(
    titulo: str,
    monto,
    moneda: str,
    vence_manana: bool,
    fecha_vencimiento: date,
    dias_anticipacion: int = 1,
) -> str:
    """Build the reminder WhatsApp message."""
    fecha_str = fecha_vencimiento.strftime("%d/%m")
    if dias_anticipacion == 3:
        msg = f"🔔 ¡Ey! En 3 días vence tu pago de *{titulo}* ({fecha_str})."
    elif vence_manana:
        msg = f"🔔 ¡Ey! Mañana vence tu pago de *{titulo}*."
    else:
        msg = f"🔔 Tu pago de *{titulo}* vence el {fecha_str}."

    if monto is not None:
        moneda = moneda or "ARS"
        msg += f"\n💰 Monto: ${_format_amount(monto)} {moneda}"

    return msg


def _persist_aviso_delivery_result(
    aviso_id,
    recordatorio_id,
    today: date,
    send_result: WhatsAppSendResult,
) -> None:
    session = SessionLocal()
    try:
        aviso = session.query(AvisoRecordatorio).filter(AvisoRecordatorio.id == aviso_id).first()
        if not aviso:
            return
        rec = session.query(Recordatorio).filter(Recordatorio.id == recordatorio_id).first()
        now_utc = datetime.now(timezone.utc)

        if send_result.status == WhatsAppDeliveryStatus.SUCCESS:
            aviso.estado = "sent"
            aviso.enviado_en = now_utc
            aviso.whatsapp_message_id = send_result.message_id
            aviso.error_detalle = None
            aviso.actualizado_en = now_utc
            if rec:
                rec.ultimo_aviso_enviado = today
            session.commit()
            logger.info(
                "[REMINDER_SENT] user_id=%s reminder=%s periodo=%s",
                aviso.usuario_id, aviso.recordatorio_id, aviso.periodo,
            )
        elif send_result.status == WhatsAppDeliveryStatus.RETRYABLE:
            if aviso.intentos < aviso.max_intentos:
                backoff_minutes = 5 * (2 ** (aviso.intentos - 1))
                aviso.estado = "failed"
                aviso.es_reintentable = True
                aviso.reintentar_en = now_utc + timedelta(minutes=backoff_minutes)
                aviso.error_detalle = send_result.error_message
            else:
                aviso.estado = "failed"
                aviso.es_reintentable = False
                aviso.error_detalle = send_result.error_message or "max_attempts_reached"
            aviso.actualizado_en = now_utc
            session.commit()
            logger.warning(
                "[REMINDER_RETRYABLE_FAIL] aviso=%s attempt=%d/%d",
                aviso.id, aviso.intentos, aviso.max_intentos,
            )
        elif send_result.status == WhatsAppDeliveryStatus.PERMANENT:
            aviso.estado = "failed"
            aviso.es_reintentable = False
            aviso.error_detalle = send_result.error_message
            aviso.actualizado_en = now_utc
            session.commit()
            logger.warning(
                "[REMINDER_PERMANENT_FAIL] aviso=%s error=%s",
                aviso.id, send_result.error_message,
            )
        else:  # UNKNOWN
            aviso.estado = "unknown"
            aviso.es_reintentable = False
            aviso.error_detalle = send_result.error_message or "delivery_status_unknown"
            aviso.actualizado_en = now_utc
            session.commit()
            logger.warning(
                "[REMINDER_UNKNOWN_STATUS] aviso=%s",
                aviso.id,
            )
    finally:
        session.close()


def _persist_manual_reminder_sent(recordatorio_id, today: date) -> None:
    session = SessionLocal()
    try:
        rec = session.query(Recordatorio).filter(Recordatorio.id == recordatorio_id).first()
        if rec:
            rec.ultimo_aviso_enviado = today
            session.commit()
    finally:
        session.close()


async def check_reminders(_now: datetime | None = None):
    """Query active reminders and send WhatsApp alerts.

    Args:
        _now: Injectable datetime for testing. Uses UTC now if not provided.
    """
    now_utc = _to_aware_utc(_now or datetime.now(timezone.utc))
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ARGENTINA_TZ)
    today = local_now.date()

    smart_candidates_to_send = []
    manual_candidates_to_send = []

    session = SessionLocal()
    try:
        # Reconciliar filas en 'sending' varadas hace más de 15 minutos (entrega at-most-once)
        reconcile_stranded_sending_claims(session, now_utc, cutoff_minutes=15)

        month_start = today.replace(day=1)

        reminders = (
            session.query(Recordatorio, Usuario)
            .join(Usuario, Recordatorio.usuario_id == Usuario.id)
            .filter(
                Recordatorio.estado == "activo",
                Recordatorio.dia_del_mes.isnot(None),
            )
            .filter(
                (Recordatorio.ultimo_aviso_enviado.is_(None))
                | (Recordatorio.ultimo_aviso_enviado < month_start)
            )
            .all()
        )

        for recordatorio, usuario in reminders:
            try:
                if not usuario.whatsapp_id:
                    continue

                dias_anticipacion = recordatorio.dias_anticipacion or 1
                alert_date = _alert_day(recordatorio.dia_del_mes, today, dias_anticipacion=dias_anticipacion)

                if alert_date != today:
                    continue

                due_date = _due_date_for_reminder(recordatorio.dia_del_mes, today, dias_anticipacion=dias_anticipacion)

                due_datetime = _build_due_datetime(due_date)
                hours_until_due = (due_datetime - local_now).total_seconds() / 3600
                vence_manana = hours_until_due <= 24

                # Manejo desacoplado e idempotente para recordatorios inteligentes (con candidato)
                if recordatorio.candidato_id is not None:
                    cand = session.query(CandidatoGastoRecurrente).filter(
                        CandidatoGastoRecurrente.id == recordatorio.candidato_id
                    ).first()
                    if not cand:
                        continue

                    periodo = due_date.strftime("%Y-%m")

                    # Buscar o crear AvisoRecordatorio con resolución segura de concurrencia
                    aviso = (
                        session.query(AvisoRecordatorio)
                        .filter(
                            AvisoRecordatorio.usuario_id == usuario.id,
                            AvisoRecordatorio.patron_hash == cand.patron_hash,
                            AvisoRecordatorio.periodo == periodo,
                        )
                        .first()
                    )

                    if aviso is None:
                        try:
                            with session.begin_nested():
                                aviso = AvisoRecordatorio(
                                    usuario_id=usuario.id,
                                    patron_hash=cand.patron_hash,
                                    periodo=periodo,
                                    recordatorio_id=recordatorio.id,
                                    estado="pendiente",
                                )
                                session.add(aviso)
                                session.flush()
                        except IntegrityError:
                            aviso = (
                                session.query(AvisoRecordatorio)
                                .filter(
                                    AvisoRecordatorio.usuario_id == usuario.id,
                                    AvisoRecordatorio.patron_hash == cand.patron_hash,
                                    AvisoRecordatorio.periodo == periodo,
                                )
                                .one()
                            )

                    if aviso.estado in ("sent", "suprimido", "unknown", "sending"):
                        continue
                    if aviso.estado == "failed":
                        if not aviso.es_reintentable or (aviso.reintentar_en and aviso.reintentar_en > now_utc) or (aviso.intentos >= aviso.max_intentos):
                            continue

                    # Evaluación determinista de supresiones
                    if not usuario.proactivo_habilitado:
                        aviso.estado = "suprimido"
                        aviso.motivo_supresion = "proactivo_deshabilitado"
                        aviso.actualizado_en = now_utc
                        session.commit()
                        logger.info(
                            "[REMINDER_SUPPRESSED] user=%s reminder=%s motivo=proactivo_deshabilitado",
                            usuario.whatsapp_id, recordatorio.id,
                        )
                        continue

                    if cand.estado in ("rechazado", "pausado", "desactivado") or recordatorio.estado in ("pausado", "eliminado"):
                        aviso.estado = "suprimido"
                        aviso.motivo_supresion = "candidato_no_activo"
                        aviso.actualizado_en = now_utc
                        session.commit()
                        logger.info(
                            "[REMINDER_SUPPRESSED] user=%s reminder=%s motivo=candidato_no_activo",
                            usuario.whatsapp_id, recordatorio.id,
                        )
                        continue

                    # Verificar si el usuario ya registró el gasto del período
                    if RecurringExpenseService.check_period_expense_registered(
                        session=session,
                        user_id=usuario.id,
                        patron_hash=cand.patron_hash,
                        reference_date=due_date,
                    ):
                        aviso.estado = "suprimido"
                        aviso.motivo_supresion = "gasto_registrado"
                        aviso.actualizado_en = now_utc
                        session.commit()
                        logger.info(
                            "[REMINDER_SUPPRESSED] user=%s reminder=%s motivo=gasto_registrado",
                            usuario.whatsapp_id, recordatorio.id,
                        )
                        continue

                    # Reclamo atómico condicional a sending: sólo una transacción obtiene rowcount == 1
                    claim_stmt = (
                        update(AvisoRecordatorio)
                        .where(
                            AvisoRecordatorio.id == aviso.id,
                            or_(
                                AvisoRecordatorio.estado == "pendiente",
                                and_(
                                    AvisoRecordatorio.estado == "failed",
                                    AvisoRecordatorio.es_reintentable.is_(True),
                                    AvisoRecordatorio.reintentar_en <= now_utc,
                                    AvisoRecordatorio.intentos < AvisoRecordatorio.max_intentos,
                                ),
                            ),
                        )
                        .values(
                            estado="sending",
                            intentos=AvisoRecordatorio.intentos + 1,
                            ultimo_intento_en=now_utc,
                            actualizado_en=now_utc,
                        )
                    )
                    claim_result = session.execute(claim_stmt)
                    session.commit()
                    if claim_result.rowcount != 1:
                        # Reclamado concurrentemente por otro worker
                        continue

                    message = _build_message(
                        titulo=recordatorio.titulo,
                        monto=recordatorio.monto,
                        moneda=recordatorio.moneda,
                        vence_manana=vence_manana,
                        fecha_vencimiento=due_date,
                        dias_anticipacion=dias_anticipacion,
                    )
                    smart_candidates_to_send.append({
                        "aviso_id": aviso.id,
                        "recordatorio_id": recordatorio.id,
                        "whatsapp_id": usuario.whatsapp_id,
                        "message": message,
                        "window_open": _window_open(usuario, now_utc),
                        "titulo": recordatorio.titulo,
                        "monto": recordatorio.monto,
                        "moneda": recordatorio.moneda,
                        "due_date": due_date,
                    })
                    continue

                # Recordatorios manuales (HU-REM-01 legacy)
                message = _build_message(
                    titulo=recordatorio.titulo,
                    monto=recordatorio.monto,
                    moneda=recordatorio.moneda,
                    vence_manana=vence_manana,
                    fecha_vencimiento=due_date,
                    dias_anticipacion=dias_anticipacion,
                )
                manual_candidates_to_send.append({
                    "recordatorio_id": recordatorio.id,
                    "whatsapp_id": usuario.whatsapp_id,
                    "message": message,
                    "window_open": _window_open(usuario, now_utc),
                    "titulo": recordatorio.titulo,
                    "monto": recordatorio.monto,
                    "moneda": recordatorio.moneda,
                    "due_date": due_date,
                })

            except Exception as exc:
                session.rollback()
                logger.warning(
                    "[REMINDER_EVAL_ERROR] reminder=%s %s: %s",
                    recordatorio.id, type(exc).__name__, exc,
                )
    except Exception as exc:
        logger.exception("[REMINDER_QUERY_ERROR] %s: %s", type(exc).__name__, exc)
    finally:
        session.close()

    # ENVÍOS DE RED: Ninguna sesión de base de datos permanece abierta durante los await de red
    for item in smart_candidates_to_send:
        if item["window_open"]:
            send_result = await send_whatsapp_message_detailed(
                item["whatsapp_id"],
                item["message"],
            )
        else:
            template_name = os.getenv("WHATSAPP_REMINDER_TEMPLATE_NAME")
            if not template_name:
                send_result = WhatsAppSendResult(
                    status=WhatsAppDeliveryStatus.PERMANENT,
                    error_message="template_missing",
                )
            else:
                due_date_text = item["due_date"].strftime("%d/%m")
                amount_text = (
                    f"${_format_amount(item['monto'])} {item['moneda'] or 'ARS'}"
                    if item["monto"] is not None
                    else "no especificado"
                )
                send_result = await send_whatsapp_message_detailed(
                    item["whatsapp_id"],
                    template_name=template_name,
                    template_parameters=[item["titulo"], due_date_text, amount_text],
                )

        await asyncio.to_thread(
            _persist_aviso_delivery_result,
            aviso_id=item["aviso_id"],
            recordatorio_id=item["recordatorio_id"],
            today=today,
            send_result=send_result,
        )

    for item in manual_candidates_to_send:
        if item["window_open"]:
            sent = await send_whatsapp_message(item["whatsapp_id"], item["message"])
        else:
            template_name = os.getenv("WHATSAPP_REMINDER_TEMPLATE_NAME")
            if not template_name:
                print(
                    f"[REMINDER_TEMPLATE_MISSING] user={item['whatsapp_id']} "
                    f"reminder={item['recordatorio_id']}"
                )
                continue
            due_date_text = item["due_date"].strftime("%d/%m")
            amount_text = (
                f"${_format_amount(item['monto'])} {item['moneda'] or 'ARS'}"
                if item["monto"] is not None
                else "no especificado"
            )
            sent = await send_whatsapp_message(
                item["whatsapp_id"],
                template_name=template_name,
                template_parameters=[item["titulo"], due_date_text, amount_text],
            )
        if sent:
            await asyncio.to_thread(_persist_manual_reminder_sent, item["recordatorio_id"], today)


def _run_daily_recurring_detection_sync(as_of_date: date | None = None) -> None:
    if as_of_date is None:
        as_of_date = datetime.now(ARGENTINA_TZ).date()

    session = SessionLocal()
    try:
        bind = session.get_bind()
        dialect_name = bind.dialect.name
        if dialect_name == "postgresql":
            # Advisory lock key canónico: 5354418701
            lock_acquired = session.execute(
                text("SELECT pg_try_advisory_lock(5354418701)")
            ).scalar()
            if not lock_acquired:
                logger.info("[DAILY_DETECTION] Advisory lock 5354418701 ocupado; otro worker está en ejecución.")
                return

            try:
                result = RecurringExpenseService.run_daily_detection(session, as_of_date=as_of_date)
                logger.info(
                    "[DAILY_DETECTION] Completado: creados=%d actualizados=%d",
                    result.metrics.candidates_created,
                    result.metrics.candidates_updated,
                )
            finally:
                session.execute(text("SELECT pg_advisory_unlock(5354418701)"))
        else:
            # Fallback en SQLite: reclamo por fila única en CronJobClaim
            today = as_of_date
            job_claim = CronJobClaim(
                job_name="daily_recurring_detection",
                fecha_ejecucion=today,
                ejecutado_en=datetime.now(timezone.utc),
            )
            try:
                with session.begin_nested():
                    session.add(job_claim)
                    session.flush()
            except IntegrityError:
                logger.info("[DAILY_DETECTION] Tarea ya reclamada para la fecha %s", today)
                return

            result = RecurringExpenseService.run_daily_detection(session, as_of_date=as_of_date)
            session.commit()
            logger.info(
                "[DAILY_DETECTION] Completado: creados=%d actualizados=%d",
                result.metrics.candidates_created,
                result.metrics.candidates_updated,
            )
    except Exception as exc:
        logger.exception("[DAILY_DETECTION_ERROR] %s: %s", type(exc).__name__, exc)
    finally:
        session.close()


async def run_daily_recurring_detection(as_of_date: date | None = None) -> None:
    """Ejecuta la detección diaria de candidatos en background de forma unificada."""
    await asyncio.to_thread(_run_daily_recurring_detection_sync, as_of_date)


async def _send_proactive_prompt(session, usuario, now_utc: datetime, today: date):
    if not _window_open(usuario, now_utc):
        return
    if not _claim_proactive_send(session, usuario.id, today):
        return

    try:
        sent = await send_whatsapp_message(usuario.whatsapp_id, PROACTIVE_PROMPT_TEXT)
    except Exception as exc:
        sent = False
        logger.warning(
            "[PROACTIVE_ERROR] user=%s %s: %s",
            usuario.whatsapp_id, type(exc).__name__, exc,
        )
    else:
        if sent:
            logger.info("[PROACTIVE_SENT] user=%s", usuario.whatsapp_id)
            return
        logger.warning("[PROACTIVE_ERROR] user=%s send returned False", usuario.whatsapp_id)

    _release_proactive_claim(session, usuario.id, today)


async def check_proactive_prompts(_now: datetime | None = None):
    hour = _proactive_hour()
    if hour is None:
        return

    now_utc = _to_aware_utc(_now or datetime.now(timezone.utc))
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ARGENTINA_TZ)
    if local_now.hour != hour:
        return

    today = local_now.date()
    session = SessionLocal()
    try:
        registered_today = exists().where(
            MovimientoFinanciero.usuario_id == Usuario.id,
            MovimientoFinanciero.fecha_movimiento == today,
            MovimientoFinanciero.anulado_en.is_(None),
        )
        candidates = (
            session.query(Usuario)
            .filter(
                Usuario.whatsapp_id.isnot(None),
                Usuario.proactivo_habilitado.is_(True),
                or_(
                    Usuario.proactivo_ultimo_envio.is_(None),
                    Usuario.proactivo_ultimo_envio != today,
                ),
                ~registered_today,
            )
            .all()
        )

        for usuario in candidates:
            try:
                await _send_proactive_prompt(session, usuario, now_utc, today)
            except Exception as exc:
                session.rollback()
                logger.warning(
                    "[PROACTIVE_ERROR] user=%s %s: %s",
                    usuario.whatsapp_id, type(exc).__name__, exc,
                )
    except Exception:
        logger.exception("[PROACTIVE_QUERY_ERROR]")
    finally:
        session.close()


def start_scheduler():
    scheduler.add_job(check_reminders, "interval", minutes=5)
    scheduler.add_job(
        check_proactive_prompts,
        "interval",
        minutes=5,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    scheduler.add_job(
        run_daily_recurring_detection,
        "cron",
        hour=3,
        minute=0,
        timezone=ARGENTINA_TZ,
    )
    scheduler.start()
    print("Scheduler iniciado (recordatorios, recordatorio proactivo cada 5 min y detección diaria 03:00).")
