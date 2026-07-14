import calendar
from datetime import datetime, date, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.api.whatsapp import send_whatsapp_message
from app.models.database import Recordatorio, SessionLocal, Usuario

scheduler = AsyncIOScheduler()

WHATSAPP_WINDOW_HOURS = 24


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


def _build_message(titulo: str, monto, moneda: str, vence_manana: bool, fecha_vencimiento: date) -> str:
    """Build the reminder WhatsApp message."""
    if vence_manana:
        msg = f"🔔 Recordatorio: mañana vence tu pago de {titulo}."
    else:
        fecha_str = fecha_vencimiento.strftime("%d/%m")
        msg = f"🔔 Recordatorio: tu pago de {titulo} vence el {fecha_str}."

    if monto is not None:
        moneda = moneda or "ARS"
        msg += f"\nMonto: ${monto} {moneda}"

    return msg


async def check_reminders(_now: datetime | None = None):
    """Query active reminders and send WhatsApp alerts.

    Args:
        _now: Injectable datetime for testing. Uses UTC now if not provided.
    """
    now = _now or datetime.now(timezone.utc)
    today = now.date() if isinstance(now, datetime) else now

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

                # Calculate due date for message
                max_day = calendar.monthrange(today.year, today.month)[1]
                effective_due_day = min(recordatorio.dia_del_mes, max_day)
                due_date = date(today.year, today.month, effective_due_day)

                # If due_date is before today (alert_day was end of prev month),
                # due date is actually in next month
                if due_date <= today:
                    next_month = today.month + 1
                    next_year = today.year
                    if next_month > 12:
                        next_month = 1
                        next_year += 1
                    max_day_next = calendar.monthrange(next_year, next_month)[1]
                    effective_due_day = min(recordatorio.dia_del_mes, max_day_next)
                    due_date = date(next_year, next_month, effective_due_day)

                # Determine if due date is within 24h
                due_datetime = datetime(
                    due_date.year, due_date.month, due_date.day,
                    tzinfo=timezone.utc,
                )
                hours_until_due = (due_datetime - now).total_seconds() / 3600
                vence_manana = hours_until_due <= 24

                message = _build_message(
                    titulo=recordatorio.titulo,
                    monto=recordatorio.monto,
                    moneda=recordatorio.moneda,
                    vence_manana=vence_manana,
                    fecha_vencimiento=due_date,
                )

                await send_whatsapp_message(usuario.whatsapp_id, message)

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


def start_scheduler():
    scheduler.add_job(check_reminders, "interval", minutes=5)
    scheduler.start()
    print("Scheduler iniciado (recordatorios cada 5 min).")
