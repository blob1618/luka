import calendar
import logging
import os
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import exists, or_

from app.api.whatsapp import send_whatsapp_message
from app.models.database import MovimientoFinanciero, Recordatorio, SessionLocal, Usuario

scheduler = AsyncIOScheduler()
logger = logging.getLogger(__name__)

WHATSAPP_WINDOW_HOURS = 24
ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
PROACTIVE_PROMPT_TEXT = "👋 ¡Ey! ¿Tuviste algún gasto hoy que no registraste? Contame y lo anoto. (Si no querés estos avisos, pedime que no te escriba más.)"


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


def _alert_day(dia_del_mes: int, reference_date: date) -> date:
    """Calculate the day to send the alert (day before next due date).

    If dia_del_mes is 1, alert goes on last day of previous month.
    Adjusts for months with fewer days and handles next-month roll.
    """
    year = reference_date.year
    month = reference_date.month

    # 1. Due date this month (adjusted for short months)
    max_day = calendar.monthrange(year, month)[1]
    effective_day = min(dia_del_mes, max_day)
    due_this_month = date(year, month, effective_day)

    # 2. If due date has passed or is today, next due is next month
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

    # 3. Alert day is the day before next due
    return next_due - timedelta(days=1)


def _due_date_for_reminder(dia_del_mes: int, today: date) -> date:
    max_day = calendar.monthrange(today.year, today.month)[1]
    due_date = date(today.year, today.month, min(dia_del_mes, max_day))
    if due_date > today:
        return due_date

    next_month = today.month + 1
    next_year = today.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    max_day_next = calendar.monthrange(next_year, next_month)[1]
    return date(next_year, next_month, min(dia_del_mes, max_day_next))


def _build_message(titulo: str, monto, moneda: str, vence_manana: bool, fecha_vencimiento: date) -> str:
    """Build the reminder WhatsApp message."""
    if vence_manana:
        msg = f"🔔 ¡Ey! Mañana vence tu pago de *{titulo}*."
    else:
        fecha_str = fecha_vencimiento.strftime("%d/%m")
        msg = f"🔔 Tu pago de *{titulo}* vence el {fecha_str}."

    if monto is not None:
        moneda = moneda or "ARS"
        msg += f"\n💰 Monto: ${_format_amount(monto)} {moneda}"

    return msg


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

    session = SessionLocal()
    try:
        # Query active reminders not yet alerted this month
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
                alert_date = _alert_day(recordatorio.dia_del_mes, today)

                if alert_date != today:
                    continue

                if not usuario.whatsapp_id:
                    continue

                due_date = _due_date_for_reminder(recordatorio.dia_del_mes, today)

                due_datetime = _build_due_datetime(due_date)
                hours_until_due = (due_datetime - local_now).total_seconds() / 3600
                vence_manana = hours_until_due <= 24

                window_open = _window_open(usuario, now_utc)

                message = _build_message(
                    titulo=recordatorio.titulo,
                    monto=recordatorio.monto,
                    moneda=recordatorio.moneda,
                    vence_manana=vence_manana,
                    fecha_vencimiento=due_date,
                )

                if window_open:
                    sent = await send_whatsapp_message(usuario.whatsapp_id, message)
                else:
                    template_name = os.getenv("WHATSAPP_REMINDER_TEMPLATE_NAME")
                    if not template_name:
                        print(
                            f"[REMINDER_TEMPLATE_MISSING] user={usuario.whatsapp_id} "
                            f"reminder={recordatorio.id}"
                        )
                        continue

                    due_date_text = due_date.strftime("%d/%m")
                    amount_text = (
                        f"${_format_amount(recordatorio.monto)} {recordatorio.moneda or 'ARS'}"
                        if recordatorio.monto is not None
                        else "no especificado"
                    )
                    sent = await send_whatsapp_message(
                        usuario.whatsapp_id,
                        template_name=template_name,
                        template_parameters=[recordatorio.titulo, due_date_text, amount_text],
                    )

                if not sent:
                    continue

                # Mark as sent
                recordatorio.ultimo_aviso_enviado = today
                session.commit()

                print(
                    f"[REMINDER_SENT] user={usuario.whatsapp_id} "
                    f"reminder={recordatorio.id} titulo={recordatorio.titulo}"
                )

            except Exception as exc:
                session.rollback()
                print(
                    f"[REMINDER_ERROR] reminder={recordatorio.id} "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

    except Exception as exc:
        print(f"[REMINDER_QUERY_ERROR] {type(exc).__name__}: {exc}")
    finally:
        session.close()


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
    scheduler.start()
    print("Scheduler iniciado (recordatorios y recordatorio proactivo cada 5 min).")
